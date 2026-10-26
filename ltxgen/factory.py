"""Film factory: film.json (story, cast, locations, props, shots) -> consistent multi-shot film via the studio.

  prepare FILM_DIR   cast (casting portrait -> turnaround -> panel), locations/props (Commons or rendered),
                     per-shot reference sheets, submit to the studio (project id saved in FILM_DIR/state.json)
  status  FILM_DIR   shot states from the studio
  qa      FILM_DIR   automated QA for every finished shot -> FILM_DIR/qa.json (+ flagged shots)
  bench   FILM_DIR   append this film's metrics (+ FILM_DIR/review.json manual scores) to studio/benchmark.json

film.json:
{"id": "F01", "title": "...", "logline": "...", "aspect": "16:9", "seed": 10000, "style": "...",
 "cast": {"NAME": {"desc": "fixed look", "image": "optional path", "commons": "optional search"}},
 "locations": {"LOC": {"desc": "...", "commons": "search" | "file": "File:X.jpg" | "prompt": "empty establishing shot"}},
 "props": {"PROP": {"desc": "...", "commons": "..." | "file": "..." | "prompt": "..."}},
 "shots": [{"id": "s01", "seconds": 6, "prompt": "...", "cast": ["NAME"], "location": "LOC", "props": [],
            "line": "expected dialogue (QA)", "lang": "ar"}]}
Run with D:\\video\\ltxgen\\.venv\\Scripts\\python.exe.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cast as castlib  # noqa: E402
import ltxgen  # noqa: E402

STUDIO = "http://localhost:7860"
BENCH = ltxgen.ROOT / "studio" / "benchmark.json"  # local results, one row per film
SYS_PY = "python"  # ltxgen.py is stdlib-only


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def api(path: str, body: dict | None = None, method: str | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(STUDIO + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token()})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def token() -> str:
    try:
        return json.loads((ltxgen.ROOT / "studio" / "studio.json").read_text("utf-8"))["token"]
    except Exception:
        return ""


def gen(args: list[str]) -> dict:
    r = subprocess.run([SYS_PY, str(HERE / "ltxgen.py"), "generate", *args], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": (r.stderr or r.stdout)[-800:]}


def frame_at(video: str, t: float, dest: Path) -> Path:
    subprocess.run([ltxgen.ffmpeg(), "-y", "-v", "error", "-ss", str(t), "-i", video, "-frames:v", "1", str(dest)],
                   check=True)
    return dest


def load(film_dir: Path) -> tuple[dict, dict]:
    film = json.loads((film_dir / "film.json").read_text("utf-8"))
    sp = film_dir / "state.json"
    state = json.loads(sp.read_text("utf-8")) if sp.exists() else {}
    return film, state


def save_state(film_dir: Path, state: dict) -> None:
    (film_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- prepare
def has_person(path: str) -> bool:
    import cv2
    import qa as qalib
    img = cv2.imread(path)
    return img is not None and len(qalib.faces(img, 20)) > 0


def asset_image(kind: str, key: str, spec: dict, film: dict, film_dir: Path) -> str:
    """Location/prop image: explicit path, Commons file/search, or a rendered still (no people)."""
    d = film_dir / "assets"
    d.mkdir(parents=True, exist_ok=True)
    for ext in (".jpg", ".png"):
        if (d / f"{kind}_{key}{ext}").exists():
            return str(d / f"{kind}_{key}{ext}")
    if spec.get("image"):
        return spec["image"]
    refs = [SYS_PY, str(HERE / "refs.py")]
    if spec.get("file"):
        r = subprocess.run(refs + ["get", spec["file"], "--out", str(d), "--name", f"{kind}_{key}"], capture_output=True, text=True)
    elif spec.get("commons"):
        r = subprocess.run(refs + ["best", spec["commons"], "--out", str(d), "--name", f"{kind}_{key}"] +
                           (["--landscape"] if kind == "loc" else []), capture_output=True, text=True)
    else:
        r = None
    if r is not None:
        try:
            res = json.loads(r.stdout)
            if res.get("ok") and kind == "prop" and has_person(res["path"]):
                # a person in a prop panel becomes an extra character (e.g. a face visible inside a helmet photo)
                log(f"{kind} {key}: Commons {res['title']} shows a person -> rejected, rendering instead")
                Path(res["path"]).rename(d / f"rejected_{kind}_{key}{Path(res['path']).suffix}")
                res = {}
            if res.get("ok"):
                log(f"{kind} {key}: Commons {res['title']} ({res['license']})")
                return res["path"]
        except json.JSONDecodeError:
            pass
        log(f"{kind} {key}: Commons failed, rendering instead")
    prompt = spec.get("prompt") or (
        f"An empty establishing shot of {spec['desc']}, no people, static camera, realistic photography."
        if kind == "loc" else f"A product photo of {spec['desc']} on a plain light grey background, studio lighting.")
    res = gen(["--preset", "standard", "--seconds", "2", "--seed", str(film.get("seed", 1)), "--aspect", "16:9",
               "--out-dir", str(d / f"render_{kind}_{key}"), "--prompt", prompt, "--no-sheet"])
    if not res.get("ok"):
        raise RuntimeError(f"{kind} {key} render failed: {res.get('error') or res.get('error_tail')}")
    return str(frame_at(res["video"], 1.0, d / f"{kind}_{key}.png"))


def ensure_cast(name: str, spec: dict, film: dict, film_dir: Path) -> str:
    key = f"{film['id']}_{name}"
    if (castlib.CAST / key / "meta.json").exists() and (castlib.CAST / key / "cutouts").exists():
        return key
    d = film_dir / "assets"
    d.mkdir(parents=True, exist_ok=True)
    src = spec.get("image")
    if not src and spec.get("commons"):
        src = asset_image("castref", name, {"commons": spec["commons"], "desc": spec["desc"]}, film, film_dir)
    # "standing upright ... arms at the sides" turns animals into bipedal creatures. Animals (spec "animal": true)
    # or any cast with a "pose" get that pose in the casting frame and the turnaround.
    pose = spec.get("pose") or ("in its natural animal posture on all four limbs, the whole body visible from nose to "
                                "tail, seen from the front at a low angle" if spec.get("animal") else None)
    if not src:
        log(f"casting {name}: rendering a portrait")
        body = (f"{pose}, with space around it, in front of a plain softly lit light grey studio wall" if pose else
                f"standing upright and facing the camera, the entire "
                f"body visible from head to feet with space above the head and below the feet, arms relaxed at the sides "
                f"with empty hands, calm "
                f"expression, in front of a plain softly lit light grey studio wall")
        res = gen(["--preset", "standard", "--seconds", "3", "--seed", str(film.get("seed", 1) + 7), "--aspect", "9:16",
                   "--out-dir", str(d / f"casting_{name}"), "--no-sheet", "--prompt",
                   f"A realistic full-length photo of {spec['desc']}, {body}. Static camera, soft even light, "
                   f"sharp focus. Audio: quiet room tone."])
        if not res.get("ok"):
            raise RuntimeError(f"casting {name} failed: {res.get('error') or res.get('error_tail')}")
        src = str(frame_at(res["video"], 0.8, d / f"casting_{name}.png"))
    log(f"turnaround {name}")
    who = ["--who", f"The {spec['desc'].split(',')[0]}, {pose},"] if pose else []
    r = subprocess.run([sys.executable, str(HERE / "cast.py"), "create", key, "--image", src, "--desc", spec["desc"],
                        "--seed", str(film.get("seed", 1) + 11), "--full", *who], capture_output=True, text=True)
    try:
        res = json.loads(r.stdout)
    except json.JSONDecodeError:
        res = {"ok": False, "error": r.stderr[-800:]}
    if not res.get("ok"):
        raise RuntimeError(f"cast {name} failed: {res}")
    return key


def role(film: dict, name: str) -> str:
    """How prompts refer to a character. Names in prompts/sheets get painted into the picture as text,
    so prompts use a role ("the boy", "the captain") from cast[NAME]["role"]."""
    return film.get("cast", {}).get(name, {}).get("role") or "the person"


def derole(text: str, film: dict) -> str:
    """Replace character names with their roles (capitalised at sentence start)."""
    for n in sorted(film.get("cast", {}), key=len, reverse=True):
        r = role(film, n)
        text = re.sub(rf"(^|[.!?:]\s+|\"\s*)\b{n}\b", lambda m: m.group(1) + r[:1].upper() + r[1:], text)
        # "an exhausted NAME" -> "an exhausted boy" (article + one adjective, not "a gift to NAME")
        noun = re.sub(r"^(the|his|her|their) ", "", r)
        text = re.sub(rf"\b([Tt])he {n}\b", lambda m: (r[:1].upper() if m.group(1) == "T" else r[:1]) + r[1:], text)
        text = re.sub(rf"\b([Aa]n? (?!(?:to|and|of|with|for|from|at|on|in|by|as|or)\b)[\w-]+ ){n}\b",
                      lambda m: m.group(1) + noun, text)
        text = re.sub(rf"\b{n}\b", r, text)
    # prop names -> "the <noun phrase>" from the prop desc ("a small red wooden toy boat, ..." ->
    # "the small red wooden toy boat"); "role" on the prop overrides.
    for p in sorted(film.get("props", {}), key=len, reverse=True):
        spec = film["props"][p]
        r = spec.get("role") or "the " + re.sub(r"^(an?|the) ", "", spec.get("desc", p.lower()).split(",")[0])
        text = re.sub(rf"(^|[.!?:]\s+)\b{p}\b", lambda m: m.group(1) + r[:1].upper() + r[1:], text)
        text = re.sub(rf"\b{p}\b", r, text)
    return text


def opening_sentence(sh: dict, film: dict) -> str:
    """Anti-leak opener for Ingredients shots: tell the model the first frame is the scene,
    not the reference sheet."""
    # per-shot "setting" overrides the location desc when weather/time differ (a storm shot at a "calm sea" location)
    loc = sh.get("setting") or film.get("locations", {}).get(sh.get("location") or "", {}).get("desc")
    rl = [role(film, n) for n in sh.get("cast", [])]
    who = " and ".join([", ".join(rl[:-1]), rl[-1]] if len(rl) > 1 else rl)
    # Close-ups: "opens directly on <wide location>" makes the model frame wide;
    # put the shot size and the person first and the location second.
    size = re.search(r"\b(extreme close-up|close-up|medium close-up)\b", sh.get("prompt", ""), re.I)
    if size and who:
        where = f" in {loc}" if loc else ""
        return f"The shot opens directly on a {size.group(1).lower()} of {who}{where}. "
    s = f"The shot opens directly on {loc}" if loc else "The shot opens directly on the scene"
    return s + (f", with {who} already in place. " if who else ". ")


EXIT = re.compile(r"\b(roll(?:s|ing)? backwards?|jump(?:s|ing)? (?:into|off|out)|dives? (?:into|off)|falls? (?:into|off|out)|"
                  r"walks? (?:away|off|out)|runs? (?:away|off|out)|leaves?|exits?|disappears?|out of (?:frame|sight|view))\b", re.I)


HANDS = re.compile(r"\bclose-up on (?:his|her|their|the \w+'s) hands\b", re.I)


def blocking(sh: dict, film: dict) -> str:
    """Multi-character shots tend to drop the 2nd character -> name who stands where."""
    # Two similar-sized roles can fuse into one person (one's clothes, the other's hair). Name each role with its
    # short look (first clause of the desc) so the model sees two different people.
    def look(n: str) -> str:
        d = film.get("cast", {}).get(n, {}).get("desc", "")
        return f"{role(film, n)} ({d.split(',')[0]})" if d else role(film, n)
    rl = [look(n) for n in sh.get("cast", [])] if len(sh.get("cast", [])) >= 2 else [role(film, n) for n in sh.get("cast", [])]
    # "visible the whole time" freezes actions where someone leaves the frame (jumping into water, walking away).
    # Exit actions keep the left/right placement but not the "whole time".
    exits = EXIT.search(sh.get("prompt", ""))
    if len(rl) == 2:
        return f"Two people in frame: {rl[0]} on the left and {rl[1]} on the right" + \
            (". " if exits else ", both clearly visible the whole time. ")
    if len(rl) >= 3:
        return f"{len(rl)} people in frame, left to right: " + ", ".join(rl) + \
            (". " if exits else ", all clearly visible the whole time. ")
    return ""


INDOOR = re.compile(r"\b(back home|at home|indoors|inside the (?:house|flat|apartment|room)|apartment|bedroom|living room|kitchen|bathroom)\b", re.I)
OUTDOOR = re.compile(r"\b(street|square|terrace|beach|sea|field|desert|road|alley|outside|outdoors|sky)\b", re.I)


CAST_STATE = re.compile(r"\b(pushed up|pulled up|holding|carrying|in (?:his|her|its) hands?|smil\w*|grin\w*|frown\w*|laugh(?!\s+lines)\w*)\b", re.I)


def lint(film: dict) -> list[str]:
    """Script checks run before anything renders.

    Indoor action with an outdoor setting: the opener sentence wins and the shot renders the empty outdoor place.
    Flag shots whose action is indoors but whose setting/location is outdoors."""
    out = []
    for sh in film.get("shots", []):
        loc = sh.get("setting") or film.get("locations", {}).get(sh.get("location") or "", {}).get("desc", "")
        if INDOOR.search(sh.get("prompt", "")) and OUTDOOR.search(loc) and not INDOOR.search(loc):
            out.append(f"{sh['id']}: indoor action but outdoor setting '{loc[:60]}' -> add an indoor 'setting'")
    out += [f"{sh['id']}: no 'seconds'" for sh in film.get("shots", []) if not sh.get("seconds")]
    # A prop mentioned by its noun ("the toy horse") instead of its key (HORSE) never joins the sheet, and the model
    # invents its own version (a real horse). Flag those so the key can be written.
    for p in film.get("props", {}):
        for sh in film.get("shots", []):
            text = re.sub(r'"[^"]*"', "", sh.get("prompt", ""))  # dialogue may mention the noun
            if sh.get("cast") and "props" not in sh and re.search(rf"\b{p.lower()}s?\b", text) and not re.search(rf"\b{p}\b", text):
                out.append(f"{sh['id']}: mentions '{p.lower()}' but not prop {p} -> write {p} so it joins the sheet")
    # A state in the cast desc ("mask pushed up on her forehead", "smiling") sticks to every shot, because the
    # reference image wins over the prompt in close-ups.
    for n, c in film.get("cast", {}).items():
        m = CAST_STATE.search(c.get("desc", ""))
        if m:
            out.append(f"cast {n}: desc fixes a state '{m.group(0)}' -> describe it per shot instead")
    return out


def cmd_prepare(a: argparse.Namespace) -> None:
    film_dir = Path(a.film_dir).resolve()
    film, state = load(film_dir)
    problems = lint(film)
    for p in problems:
        log("LINT " + p)
    if problems and not getattr(a, "force", False):
        raise SystemExit("lint failed (fix the film.json or pass --force)")
    # Casting/asset renders use the same GPU as the studio worker; two pipelines at once starve RAM and stall.
    # Only wait when something actually has to be rendered (all casts + assets cached -> no wait).
    def cached(kind: str, key: str) -> bool:
        return any((film_dir / "assets" / f"{kind}_{key}{e}").exists() for e in (".jpg", ".png"))
    need_gpu = any(not (castlib.CAST / f"{film['id']}_{n}" / "meta.json").exists() for n in film.get("cast", {})) or \
        any(not cached("loc", k) for k in film.get("locations", {})) or \
        any(not cached("prop", k) for k in film.get("props", {}))
    while need_gpu:
        try:
            projs = api("/api/projects")
            projs = projs if isinstance(projs, list) else projs.get("projects", [])
            busy = [p["id"] for p in projs if p.get("status") in ("rendering", "stitching")
                    and (p.get("counts", {}).get("ready", 0) or p.get("counts", {}).get("claimed", 0))]
        except Exception:
            busy = []
        if not busy:
            break
        log(f"studio busy ({', '.join(busy)}): waiting before casting/asset renders")
        time.sleep(60)
    cast_keys = {n: ensure_cast(n, s, film, film_dir) for n, s in film.get("cast", {}).items()}
    locs = {k: asset_image("loc", k, s, film, film_dir) for k, s in film.get("locations", {}).items()}
    props = {k: asset_image("prop", k, s, film, film_dir) for k, s in film.get("props", {}).items()}
    if getattr(a, "assets_only", False):  # stop for a visual check of panels/locations/props before submit
        review = film_dir / "assets_review.jpg"
        imgs = [castlib.CAST / k / "panel.png" for k in cast_keys.values()] + [Path(p) for p in locs.values()] + \
            [Path(p) for p in props.values()]
        from PIL import Image
        tiles = []
        for p in imgs:
            im = Image.open(p).convert("RGB")
            im.thumbnail((900, 420))
            tiles.append(im)
        W = max(t.width for t in tiles)
        sheet = Image.new("RGB", (W, sum(t.height + 8 for t in tiles)), (30, 30, 30))
        y = 0
        for t in tiles:
            sheet.paste(t, (0, y))
            y += t.height + 8
        sheet.save(review, quality=88)
        print(json.dumps({"ok": True, "review": str(review), "cast": cast_keys, "locations": locs, "props": props}))
        return
    # Location strip in the sheet: a sheet that is mostly light-grey cells can bleed into the video as a milky haze;
    # a location strip reduces haze but makes layout leaks more likely (QA detects and autofix redoes those).
    # Default is off (film "sheet_location": true turns it on); see build_shot.
    shots = [build_shot(film, sh, i, cast_keys, locs, props, film_dir) for i, sh in enumerate(film["shots"])]
    sb = {"title": f"{film['id']} {film['title']}", "preset": "standard", "aspect": film.get("aspect", "16:9"),
          "seed": film.get("seed", 1000), "style": film.get("style", ""), "transition": film.get("transition", "cut"),
          "fade_seconds": 0.3, "output_size": film.get("output_size", [1280, 704]), "shots": shots}
    (film_dir / "storyboard.studio.json").write_text(json.dumps(sb, ensure_ascii=False, indent=2), encoding="utf-8")
    if a.no_submit:
        print(json.dumps({"ok": True, "storyboard": str(film_dir / "storyboard.studio.json")}))
        return
    pid = api("/api/projects", sb)["project_id"]
    state.update(project_id=pid, submitted=time.time(), cast=cast_keys, locations=locs, props=props)
    save_state(film_dir, state)
    print(json.dumps({"ok": True, "project_id": pid, "shots": len(shots),
                      "ingredients_shots": sum(1 for s in shots if "ingredients" in s)}))


def build_shot(film: dict, sh: dict, i: int, cast_keys: dict, locs: dict, props: dict, film_dir: Path) -> dict:
    """film.json shot -> studio storyboard shot (roles, opener, blocking, reference sheet). Shared by prepare + reshot."""
    use_loc = film.get("sheet_location", False)  # off by default: fewer layout leaks
    if True:
        st = {"id": sh["id"], "seconds": sh.get("seconds", 6), "prompt": derole(sh["prompt"], film),
              "seed": sh.get("seed", film.get("seed", 1000) + i)}
        # a cast NAME mentioned in the prompt must be on the sheet, else the model invents a stranger
        for n in film.get("cast", {}):
            if re.search(rf"\b{n}\b", sh["prompt"]) and n not in sh.setdefault("cast", []):
                sh["cast"].append(n)
                log(f"{sh['id']}: added {n} to cast (named in prompt)")
        # same for props: a prop KEY in the prompt joins the sheet (when there are characters) and derole()
        # replaces the key with the prop's noun phrase (a bare key would be read literally).
        explicit = "props" in sh  # an explicit list (even []) is a deliberate choice (e.g. the holder is off screen)
        for p in film.get("props", {}):
            if not explicit and re.search(rf"\b{p}\b", sh["prompt"]) and p not in sh.setdefault("props", []):
                sh["props"].append(p)
                log(f"{sh['id']}: added prop {p} (named in prompt)")
        names = [cast_keys[n] for n in sh.get("cast", [])]
        sprops = sh.get("props", [])
        loc = sh.get("location")
        # prop-only shots (no character) render from text: their sheets tend to freeze as the prop photo itself.
        # film "prop_only_sheets": true re-enables them.
        # Hands-only close-ups with a sheet tend to reproduce the whole sheet for the full clip. Render them from
        # text, with each character's short look so sleeves/cuffs still match.
        if names and HANDS.search(sh["prompt"]):
            looks = "; ".join(f"{role(film, n)}: {film['cast'][n]['desc'].split(',')[0]}" for n in sh.get("cast", []))
            st["prompt"] = f"({looks}.) " + st["prompt"]
            log(f"{sh['id']}: hands close-up -> text only")
        elif names or (sprops and film.get("prop_only_sheets", False)):
            sheet = castlib.compose_sheet(
                names, locs.get(loc) if loc and names and use_loc and film["locations"][loc].get("in_sheet", True) else None, [props[p] for p in sprops], film_dir / "sheets" / f"{sh['id']}.png",
                film["locations"][loc]["desc"] if loc and names and use_loc and film["locations"][loc].get("in_sheet", True) else None, [film["props"][p]["desc"] for p in sprops],
                [role(film, n) for n in sh.get("cast", [])])
            st["ingredients"] = {"sheet": sheet["sheet"], "desc": sheet["sheet_desc"], "strength": 1.0}
            if names:  # opener only with characters: on prop close-ups it pulls the shot back to the wide location
                st["prompt"] = opening_sentence(sh, film) + blocking(sh, film) + st["prompt"]
    return st


def cmd_finish(a: argparse.Namespace) -> None:
    """Copy the stitched studio film into the film folder (final.mp4, "FXX Title.mp4") + contact sheet."""
    film_dir = Path(a.film_dir).resolve()
    film, state = load(film_dir)
    src = ltxgen.ROOT / "studio" / "projects" / state["project_id"] / "final.mp4"
    if not src.exists():
        raise SystemExit("no final.mp4 yet (not all shots are done)")
    shutil.copyfile(src, film_dir / "final.mp4")
    safe = re.sub(r'[<>:"/\\|?*]', "", f"{film['id']} {film['title']}")
    shutil.copyfile(src, film_dir.parent / f"{safe}.mp4")
    ltxgen.make_sheet(film_dir / "final.mp4", film_dir / "final_sheet.jpg", cols=6, rows=3)
    print(json.dumps({"ok": True, "final": str(film_dir / "final.mp4"), "duration_s": ltxgen.probe(src).get("duration_s")}))


def cmd_recast(a: argparse.Namespace) -> None:
    """Forget a cast member / location / prop render so the next prepare re-creates it (web app 'Recast')."""
    film_dir = Path(a.film_dir).resolve()
    film, _ = load(film_dir)
    if ":" in a.what:
        kind, key = a.what.split(":", 1)
        for f in (film_dir / "assets").glob(f"{kind}_{key}*"):
            shutil.rmtree(f) if f.is_dir() else f.unlink()
    else:
        shutil.rmtree(castlib.CAST / f"{film['id']}_{a.what}", ignore_errors=True)
        for f in (film_dir / "assets").glob(f"casting_{a.what}*"):
            shutil.rmtree(f) if f.is_dir() else f.unlink()
    print(json.dumps({"ok": True, "forgot": a.what}))


def cmd_reshot(a: argparse.Namespace) -> None:
    """Rebuild one shot from the (edited) film.json and re-render it in the studio project (web app 'Edit & redo')."""
    film_dir = Path(a.film_dir).resolve()
    film, state = load(film_dir)
    i, sh = next((i, s) for i, s in enumerate(film["shots"]) if s["id"] == a.shot)
    if a.seed is not None:
        sh["seed"] = a.seed
    st = build_shot(film, sh, i, state.get("cast", {}), state.get("locations", {}), state.get("props", {}), film_dir)
    if a.no_sheet:
        st.pop("ingredients", None)
    body = {"prompt": st["prompt"], "seed": st["seed"], "seconds": st["seconds"],
            "ingredients": st.get("ingredients")}
    res = api(f"/api/projects/{state['project_id']}/shots/{a.shot}/redo", body)
    print(json.dumps({"ok": True, "shot": a.shot, "seed": st["seed"], "sheet": bool(st.get("ingredients")), **res}))


# ---------------------------------------------------------------- status / qa / bench
def project(state: dict) -> dict:
    if not state.get("project_id"):
        raise SystemExit("this film has not been sent to render yet")
    return api(f"/api/projects/{state['project_id']}")


def shot_video(pid: str, s: dict) -> Path | None:
    if not s.get("video"):
        return None
    return ltxgen.ROOT / "studio" / "projects" / pid / "shots" / s["id"] / f"v{s['version']}" / "video.mp4"


def cmd_status(a: argparse.Namespace) -> None:
    _, state = load(Path(a.film_dir))
    p = project(state)
    counts: dict[str, int] = {}
    for s in p["shots"]:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    print(json.dumps({"project": state["project_id"], "status": p["status"], "counts": counts,
                      "final": p.get("final")}, indent=2))


def script_lang(text: str) -> str | None:
    """Whisper language from the line's script. Forcing "en" on Japanese makes Whisper TRANSLATE (every line
    scores CER 1.0 while the audio is correct Japanese). Latin script -> None (auto-detect: en/es/it/...)."""
    if re.search(r"[唔咗哋嘅喺冇咁啲嗰佢乜嚟]", text):  # Cantonese-only characters (forcing "zh" gives Mandarin)
        return "yue"
    for pat, lang in ((r"[؀-ۿ]", "ar"), (r"[぀-ヿ]", "ja"), (r"[가-힯]", "ko"),
                      (r"[一-鿿]", "zh"), (r"[ऀ-ॿ]", "hi"), (r"[Ѐ-ӿ]", "ru")):
        if re.search(pat, text):
            return lang
    return None


def cmd_qa(a: argparse.Namespace) -> None:
    import qa as qalib
    film_dir = Path(a.film_dir).resolve()
    film, state = load(film_dir)
    p = project(state)
    by_id = {s["id"]: s for s in film["shots"]}
    for sp in film["shots"]:  # quoted dialogue in the prompt is the expected line unless given explicitly
        if not sp.get("line"):
            q = re.findall(r'"([^"]{2,120})"', sp["prompt"])
            if q:
                sp["line"] = " ".join(q)
                # mixed-language films: Whisper forced to the film's dialogue_lang turns lines in other languages into
                # nonsense in that language (or hallucinated subtitle credits on short English lines).
                # Order: explicit lang > the line's own script > "in English" before an ASCII line > film language.
                eng = re.search(r'\bin English\b', sp["prompt"], re.I) or \
                    (len(q) == 1 and not re.search(r'\bin [A-Z][a-z]+\b', sp["prompt"]) and sp["line"].isascii()
                     and film.get("dialogue_lang") not in (None, "en"))
                sp["lang"] = sp.get("lang") or script_lang(sp["line"]) or ("en" if eng else None) \
                    or film.get("dialogue_lang")
    # Whisper (~2 GB) loaded during a render can starve RAM and crash the LTX text-encoder load.
    # --no-speech (used while shots are rendering) skips it; speech is scored at film end. Loaded lazily.
    _tr = {}

    def tr(video, lang):
        if "m" not in _tr:
            _tr["m"] = qalib.whisper_transcriber()
        return _tr["m"](video, lang)
    old = json.loads((film_dir / "qa.json").read_text("utf-8")) if (film_dir / "qa.json").exists() else {}
    results = {}
    for s in p["shots"]:
        v = shot_video(state["project_id"], s)
        if not v or not v.exists():
            continue
        key = f"{s['id']}@v{s['version']}"
        spec = by_id.get(s["id"], {})
        cached = old.get("shots", {}).get(key)
        if cached and not a.force and (a.no_speech or not spec.get("line") or "heard" in cached):
            results[key] = cached
            continue
        cast = [state["cast"][n] for n in spec.get("cast", [])]
        sheet = next((x for x in (film_dir / "sheets" / f"{s['id']}_noloc.png", film_dir / "sheets" / f"{s['id']}.png")
                      if x.exists()), None)
        r = qalib.shot_qa(v, cast, sheet, None if a.no_speech else spec.get("line"), spec.get("lang"), tr)
        r["elapsed_s"] = s.get("elapsed_s")
        flags = []
        leak_s = r.get("leak_seconds") or 0
        if leak_s > 0.5:
            # a leak that only covers the opening and ends in a hard cut is trimmed by the stitch (fixes 30/41)
            r["intro_cut"] = ltxgen.intro_cut(v, window=2.5)
            if leak_s <= 2.5 and r["intro_cut"] >= leak_s - 0.4:
                flags.append("intro_leak_trimmed")
            elif leak_s > 1.25:  # FPs (grilles, cages) score <= 1.0 s, real leaks >= 2 s
                flags.append("sheet_leak")
        for n, sim in (r.get("face_sim") or {}).items():
            if sim is not None and sim < 0.36:
                flags.append(f"face:{n}")
        if r["motion"] < 0.003:
            flags.append("frozen")
        if r.get("bars"):
            flags.append("bars")
        # intended night shots are dark by design;
        # only flag them when they are almost entirely black.
        night = re.search(r"\b(night|at dusk|dark)\b", (spec.get("setting") or "") + " " + spec.get("prompt", ""), re.I)
        if r["black_ratio"] > (0.6 if night else 0.2):
            flags.append("black")
        if r.get("speech_cer") is not None and r["speech_cer"] > 0.6:
            flags.append("speech")
        r["flags"] = flags
        results[key] = r
        log(f"QA {key}: flags={flags} face={r.get('face_sim')} leak={r.get('sheet_leak')}")
    (film_dir / "qa.json").write_text(json.dumps({"shots": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    # trim only confirmed sheet-leak intros (current versions) at stitch time
    trims = {}
    for sh in p["shots"]:
        r = results.get(f"{sh['id']}@v{sh['version']}") or {}
        if "intro_leak_trimmed" in r.get("flags", []) and r.get("intro_cut"):
            trims[sh["id"]] = r["intro_cut"]
        if by_id.get(sh["id"], {}).get("trim"):  # manual trim from review (single-cell leaks the detector misses)
            trims[sh["id"]] = float(by_id[sh["id"]]["trim"])
    tf = ltxgen.ROOT / "studio" / "projects" / state["project_id"] / "trims.json"
    tf.write_text(json.dumps(trims, indent=2), encoding="utf-8")
    flagged = {k: v["flags"] for k, v in results.items() if v["flags"]}
    print(json.dumps({"ok": True, "checked": len(results), "flagged": flagged}, ensure_ascii=False, indent=2))


def cmd_autofix(a: argparse.Namespace) -> None:
    """Redo shots whose latest version leaked the reference sheet: rebuild the sheet without the
    location strip, keep the opener, new seed. At most 2 automatic attempts per shot (then it needs a human review)."""
    film_dir = Path(a.film_dir).resolve()
    film, state = load(film_dir)
    qa_res = json.loads((film_dir / "qa.json").read_text("utf-8"))["shots"] if (film_dir / "qa.json").exists() else {}
    sb = json.loads((film_dir / "storyboard.studio.json").read_text("utf-8"))
    by_id = {s["id"]: s for s in film["shots"]}
    tries = state.setdefault("autofix", {})
    done = []
    for s in project(state)["shots"]:
        r = qa_res.get(f"{s['id']}@v{s['version']}")
        bad = {"sheet_leak", "frozen", "bars"} & set(r.get("flags", [])) if r else set()
        if s["status"] != "done" or not bad or tries.get(s["id"], 0) >= 2:
            continue
        spec, sbs = by_id[s["id"]], next(x for x in sb["shots"] if x["id"] == s["id"])
        names = [state["cast"][n] for n in spec.get("cast", [])]
        sprops = spec.get("props", [])
        tries[s["id"]] = tries.get(s["id"], 0) + 1
        seed = int(sbs.get("seed", 1000)) + 7919 * tries[s["id"]]
        body = {"prompt": sbs["prompt"], "seed": seed}
        how = "same sheet"
        if not names and "ingredients" in sbs:  # prop-only shot: drop the sheet entirely
            body["ingredients"] = None
            how = "no sheet (prop-only)"
        if tries[s["id"]] >= 2 and names:  # 1st retry: new seed only (no-location sheets can haze)
            sheet = castlib.compose_sheet(names, None, [state["props"][p] for p in sprops],
                                          film_dir / "sheets" / f"{s['id']}_noloc.png", None,
                                          [film["props"][p]["desc"] for p in sprops],
                                          [role(film, n) for n in spec.get("cast", [])])
            body["ingredients"] = {"sheet": sheet["sheet"], "desc": sheet["sheet_desc"], "strength": 1.0}
            how = "no-location sheet"
        api(f"/api/projects/{state['project_id']}/shots/{s['id']}/redo", body)
        done.append(s["id"])
        log(f"autofix {s['id']}: leak {r.get('leak_seconds')} s -> {how}, seed {seed}")
    save_state(film_dir, state)
    print(json.dumps({"ok": True, "redone": done}))


def cmd_bench(a: argparse.Namespace) -> None:
    film_dir = Path(a.film_dir).resolve()
    film, state = load(film_dir)
    p = project(state)
    qa = json.loads((film_dir / "qa.json").read_text("utf-8"))["shots"]
    current = {f"{s['id']}@v{s['version']}" for s in p["shots"]}
    cur = [v for k, v in qa.items() if k in current]
    sims = [x for v in cur for x in (v.get("face_sim") or {}).values() if x is not None]
    cers = [v["speech_cer"] for v in cur if v.get("speech_cer") is not None]
    leaks = [v.get("leak_seconds") or 0 for v in qa.values() if "leak_seconds" in v]  # all rendered versions
    review = json.loads((film_dir / "review.json").read_text("utf-8")) if (film_dir / "review.json").exists() else {}
    row = {
        "film": film["id"], "title": film["title"], "finished": time.strftime("%Y-%m-%d %H:%M"),
        "duration_s": (p.get("final_info") or {}).get("duration_s"), "shots": len(p["shots"]),
        # versions include queued resubmits that never rendered; review.json "rendered_versions" is exact
        "redos": (review["rendered_versions"] - len(p["shots"])) if review.get("rendered_versions") else sum(max(0, s["version"] - 1) for s in p["shots"]),
        "render_min": round(sum(v.get("elapsed_s") or 0 for v in qa.values()) / 60, 1),
        "face_consistency": round(sum(1 for x in sims if x >= 0.36) / len(sims), 3) if sims else None,
        "face_sim_mean": round(sum(sims) / len(sims), 3) if sims else None,
        "sheet_leak_rate": round(sum(1 for x in leaks if x > 0.5) / len(leaks), 3) if leaks else None,
        "final_leaks": sum(1 for v in cur if "sheet_leak" in (v.get("flags") or [])),  # trimmed intro leaks excluded
        "speech_cer_mean": round(sum(cers) / len(cers), 3) if cers else None,
        "flagged_final": sum(1 for v in cur if v.get("flags")),
        "review": review,
    }
    bp = BENCH
    bp.parent.mkdir(parents=True, exist_ok=True)
    bench = json.loads(bp.read_text("utf-8")) if bp.exists() else {"films": []}
    bench["films"] = [r for r in bench["films"] if r["film"] != film["id"]] + [row]
    bp.write_text(json.dumps(bench, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, **row}, ensure_ascii=False, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("prepare", cmd_prepare), ("status", cmd_status), ("qa", cmd_qa), ("autofix", cmd_autofix),
                     ("bench", cmd_bench), ("reshot", cmd_reshot), ("finish", cmd_finish), ("recast", cmd_recast)):
        s = sub.add_parser(name)
        s.add_argument("film_dir")
        if name == "reshot":
            s.add_argument("shot")
            s.add_argument("--seed", type=int)
            s.add_argument("--no-sheet", action="store_true", help="render from text only (no reference sheet)")
        if name == "recast":
            s.add_argument("what", help="cast NAME, loc:KEY or prop:KEY")
        if name == "prepare":
            s.add_argument("--no-submit", action="store_true")
            s.add_argument("--assets-only", action="store_true", help="cast + assets + assets_review.jpg, no submit")
            s.add_argument("--force", action="store_true", help="submit despite lint warnings")
        if name == "qa":
            s.add_argument("--force", action="store_true")
            s.add_argument("--no-speech", action="store_true", help="skip Whisper (use while the GPU renders)")
        s.set_defaults(fn=fn)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
