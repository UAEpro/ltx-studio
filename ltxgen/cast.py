"""Cast library: keep the same face/look for a character across shots and films (LTX-2.5 Ingredients IC-LoRA).

A character is saved once in D:\\video\\cast\\<NAME>\\ and reused by every later shot:
  create  NAME --image FRAME.png --desc "..."   AI turnaround clip from one frame -> panel.png
  panel   NAME [--video turnaround.mp4]        (re)build panel.png from the turnaround clip
  sheet   NAME [NAME ...] --out sheet.png [--location IMG] [--prop IMG ...]
                                               compose an Ingredients reference sheet (black bg, no text)
  list                                         show the library
Run with D:\\video\\ltxgen\\.venv\\Scripts\\python.exe (needs rembg). Prints one JSON object.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ltxgen  # noqa: E402

CAST = ltxgen.ROOT / "cast"
SHEET_W, SHEET_H = 1536, 896
VIEW_AT = (0.30, 0.50, 0.62, 0.95)    # legacy close-up turnaround: where the 4 body views are taken
# casting v2 ("full"): full-length 9:16 casting frame -> turn a full circle -> push in to a face close-up.
FULL_VIEW_AT = (0.0, 0.22, 0.38, 0.55)  # front, side, back, other side (full body)
FULL_FACE_AT = (1.0, 0.0)               # face close-up at the end of the push-in, small face from frame 0
TURN_PROMPT_FULL = ("{who} stands still for a moment, then slowly turns around in place in a full circle, showing the "
                    "left side, the back, the right side, and faces the camera again; then the camera slowly pushes in "
                    "to a sharp close-up of the face. Plain light grey studio background, even soft light, the whole "
                    "body visible until the push-in. Audio: quiet room tone.")
TURN_PROMPT = ("{who} steps back and slowly turns around in place in a full circle: first the left side profile, "
               "then the back, then the right side profile, then faces the camera again. The camera slowly pulls "
               "back to show the whole body from head to feet, standing on flat ground in front of a plain soft "
               "background. Static camera, even soft light. Audio: quiet ambience.")
_session = None
_yunet = None


def out(obj: dict, code: int = 0) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    sys.exit(code)


def cutout(img: Image.Image) -> Image.Image:
    """RGBA with background removed (BiRefNet portrait model)."""
    global _session
    from rembg import new_session, remove
    if _session is None:
        _session = new_session("birefnet-portrait")
    return remove(img.convert("RGB"), session=_session)


def bbox(rgba: Image.Image, thr: int = 128) -> tuple[int, int, int, int] | None:
    a = np.asarray(rgba)[..., 3]
    ys, xs = np.where(a > thr)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def frames(video: Path, n: int) -> list[Image.Image]:
    info = ltxgen.probe(video)
    total = max(info.get("frames") or 121, 2)
    idx = [round(i * (total - 1) / (n - 1)) for i in range(n)]
    imgs = []
    for i in idx:
        r = subprocess.run([ltxgen.ffmpeg(), "-v", "error", "-i", str(video), "-vf", f"select=eq(n\\,{i})",
                            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, check=True)
        from io import BytesIO
        imgs.append(Image.open(BytesIO(r.stdout)).convert("RGB"))
    return imgs


def on_black(rgba: Image.Image, box: tuple[int, int, int, int], size: tuple[int, int], pad: float = 0.06) -> Image.Image:
    """Crop rgba to box, fit into size on black, keeping aspect."""
    crop = rgba.crop(box)
    w, h = size
    s = min(w * (1 - 2 * pad) / crop.width, h * (1 - 2 * pad) / crop.height)
    crop = crop.resize((max(1, int(crop.width * s)), max(1, int(crop.height * s))), Image.LANCZOS)
    canvas = Image.new("RGB", size, (0, 0, 0))
    canvas.paste(crop, ((w - crop.width) // 2, (h - crop.height) // 2), crop)
    return canvas


def head_box(rgba: Image.Image, b: tuple[int, int, int, int], frac: float) -> tuple[int, int, int, int]:
    """Square head-and-shoulders crop: the subject's pixels in the top `frac` of its bbox, squared and padded."""
    top = (b[0], b[1], b[2], b[1] + int((b[3] - b[1]) * frac))
    sub = bbox(rgba.crop(top)) or (0, 0, top[2] - top[0], top[3] - top[1])
    x0, y0, x1, y1 = top[0] + sub[0], top[1] + sub[1], top[0] + sub[2], top[1] + sub[3]
    side = int(max(x1 - x0, y1 - y0) * 1.08)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    W, H = rgba.size
    x0, y0 = max(0, min(W - side, cx - side // 2)), max(0, min(H - side, cy - side // 2))
    return x0, y0, min(W, x0 + side), min(H, y0 + side)


def facing(img: Image.Image) -> tuple[str, float]:
    """('front'|'profile'|'none', yaw proxy) from YuNet landmarks: nose offset from the eye midpoint / eye distance."""
    import cv2
    global _yunet
    if "_yunet" not in globals() or _yunet is None:
        _yunet = cv2.FaceDetectorYN.create(str(ltxgen.ROOT / "models" / "qa" / "face_detection_yunet_2023mar.onnx"),
                                           "", (320, 320), 0.6)
    a = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    _yunet.setInputSize((a.shape[1], a.shape[0]))
    _, f = _yunet.detect(a)
    if f is None or not len(f):
        return "none", 0.0
    f = max(f, key=lambda r: r[2] * r[3])
    rx, ry, lx, ly, nx = f[4], f[5], f[6], f[7], f[8]
    eye_d = max(abs(lx - rx), 1.0)
    yaw = (nx - (rx + lx) / 2) / eye_d
    return ("front" if abs(yaw) < 0.18 else "profile"), float(yaw)


def adaptive_views(imgs: list[Image.Image], boxes: list) -> list[int] | None:
    """front / side / back / other side from where the subject actually faces (full-body frames only)."""
    H = imgs[0].height
    full = [i for i, b in enumerate(boxes) if b and b[1] > 2 and (b[3] - b[1]) > 0.5 * H]  # head not cut = not push-in
    if len(full) < 4:
        return None
    face = {i: facing(imgs[i]) for i in full}
    fronts = [i for i in full if face[i][0] == "front"]
    backs = [i for i in full if face[i][0] == "none"]
    profiles = [i for i in full if face[i][0] == "profile"]
    if not fronts or not backs:
        return None
    back = backs[len(backs) // 2]
    before = [i for i in profiles if i < back] or [i for i in full if i < back and i not in fronts]
    after = [i for i in profiles if i > back] or [i for i in full if i > back and i not in fronts]
    side1 = before[len(before) // 2] if before else None
    side2 = after[len(after) // 2] if after else None
    pick = [fronts[0], side1, back, side2]
    return [p if p is not None else back for p in pick]


def build_panel(name: str, video: Path, mode: str = "legacy") -> dict:
    """Cut out 2 face close-ups + 4 turnaround views from the turnaround clip; also save a preview panel."""
    d = CAST / name
    imgs = frames(video, 24)
    cuts = [cutout(im) for im in imgs]
    boxes = [bbox(c) for c in cuts]
    n = len(imgs)
    pick = [min(n - 1, round(f * (n - 1))) for f in (FULL_VIEW_AT if mode == "full" else VIEW_AT)]
    if mode == "full":
        pick = adaptive_views(imgs, boxes) or pick
    pick = [i if boxes[i] else 0 for i in pick]
    cdir = d / "cutouts"
    cdir.mkdir(exist_ok=True)
    for old_f in cdir.glob("*.png"):
        old_f.unlink()
    # faces: the source frame (front) and an early frame of the turn (three-quarter); the source frame is
    # usually a close-up, so its "head" is most of the bbox
    faces = []
    face_frames = [min(n - 1, round(f * (n - 1))) for f in FULL_FACE_AT] if mode == "full" else [0, 2]
    for k, i in enumerate(face_frames):
        b = boxes[i] or boxes[0]
        if mode == "full":   # end of the push-in = close-up; frame 0 = full body (feet may touch the edge)
            close_up = i != 0
        else:
            close_up = (b[3] - b[1]) > 0.7 * imgs[0].height and b[3] >= imgs[0].height - 3  # cut by frame bottom
        fb = head_box(cuts[i], b, 0.9 if close_up else (0.2 if mode == "full" else 0.3))
        cuts[i].crop(fb).save(cdir / f"face_{k}.png")
        faces.append(f"face_{k}.png")
    bodies = []
    for k, i in enumerate(pick):
        cuts[i].crop(boxes[i]).save(cdir / f"body_{k}.png")
        bodies.append(f"body_{k}.png")
    panel = grid_row([Image.open(cdir / f) for f in faces], [Image.open(cdir / f) for f in bodies], 1536, 448)
    panel.save(d / "panel.png")
    return {"panel": str(d / "panel.png"), "faces": faces, "bodies": bodies, "views_from_frames": pick, "mode": mode}


def cmd_create(a: argparse.Namespace) -> None:
    d = CAST / a.name
    d.mkdir(parents=True, exist_ok=True)
    src = d / "source.png"
    Image.open(a.image).convert("RGB").save(src)
    meta = {"name": a.name, "desc": a.desc, "source_image": a.image}
    who = a.who or f"The {a.desc.split(',')[0]}"
    mode = "full" if a.full else "legacy"
    tmpl = TURN_PROMPT_FULL if a.full else TURN_PROMPT
    r = subprocess.run([sys.executable, str(HERE / "ltxgen.py"), "generate", "--preset", "standard", "--seconds", "6",
                        "--aspect", "9:16" if a.full else "16:9",
                        "--seed", str(a.seed), "--image", str(src), "0", "1.0", "--out-dir", str(d / "turnaround"),
                        "--name", f"cast-{a.name}", "--prompt", tmpl.format(who=who)],
                       capture_output=True, text=True)
    res = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {"ok": False, "error": r.stderr[-800:]}
    if not res.get("ok"):
        out({"ok": False, "step": "turnaround", **res}, 1)
    meta["turnaround"] = res["video"]
    meta.update(build_panel(a.name, Path(res["video"]), mode))
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    out({"ok": True, **meta})


def cmd_panel(a: argparse.Namespace) -> None:
    d = CAST / a.name
    meta = json.loads((d / "meta.json").read_text("utf-8")) if (d / "meta.json").exists() else {"name": a.name}
    video = Path(a.video or meta.get("turnaround") or d / "turnaround" / "video.mp4")
    meta.update(build_panel(a.name, video, meta.get("mode", "legacy")))
    meta["turnaround"] = str(video)
    if a.desc:
        meta["desc"] = a.desc
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    out({"ok": True, **meta})


def fit(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    w, h = size
    s = min(w / img.width, h / img.height)
    im = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.LANCZOS)
    c = Image.new("RGB", size, (0, 0, 0))
    c.paste(im, ((w - im.width) // 2, (h - im.height) // 2))
    return c


GUTTER, STUDIO = 6, (214, 214, 214)   # thin black gutters, light-grey studio cells (as in the official sheets)


def cell(rgba: Image.Image, size: tuple[int, int], pad: float = 0.04) -> Image.Image:
    """Subject cutout filling a light-grey studio cell."""
    w, h = size
    c = Image.new("RGB", size, STUDIO)
    if rgba.mode != "RGBA":
        rgba = rgba.convert("RGBA")
    s = min(w * (1 - 2 * pad) / rgba.width, h * (1 - 2 * pad) / rgba.height)
    im = rgba.resize((max(1, int(rgba.width * s)), max(1, int(rgba.height * s))), Image.LANCZOS)
    c.paste(im, ((w - im.width) // 2, (h - im.height) // 2), im)
    return c


def cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Crop-fill (no letterbox) for location panels."""
    w, h = size
    s = max(w / img.width, h / img.height)
    im = img.convert("RGB").resize((int(img.width * s) + 1, int(img.height * s) + 1), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


def grid_row(faces: list[Image.Image], bodies: list[Image.Image], width: int, height: int) -> Image.Image:
    """One character row: face close-ups (wide cells) then full-body views (narrow cells), filling the row."""
    row = Image.new("RGB", (width, height), (0, 0, 0))
    fw = min(int(height * 0.85), int(width * 0.2))  # faces never take more than ~40% of the row
    widths = [fw] * len(faces)
    rest = width - sum(widths) - GUTTER * (len(faces) + len(bodies) - 1)
    widths += [rest // max(1, len(bodies))] * len(bodies)
    x = 0
    for im, w in zip(faces + bodies, widths):
        row.paste(cell(im, (w, height)), (x, 0))
        x += w + GUTTER
    return row


def compose_sheet(names: list[str], location: str | None, props: list[str], dest: Path,
                  location_desc: str | None = None, prop_descs: list[str] | None = None, roles=None) -> dict:
    """Dense reference sheet: one row per character (2 face close-ups + 4 turnaround views), an optional props
    row, and a full-width location strip at the bottom. Returns a position-aware text description."""
    rows: list[tuple[str, float]] = [("char", 1.0)] * len(names)
    if props:
        rows.append(("props", 0.7))
    if location:
        rows.append(("loc", 1.15 if names else 2.0))
    total = sum(w for _, w in rows)
    avail = SHEET_H - GUTTER * (len(rows) - 1)
    sheet = Image.new("RGB", (SHEET_W, SHEET_H), (0, 0, 0))
    y, parts, ci = 0, [], 0
    labels = ["Top row", "Second row", "Third row", "Fourth row", "Fifth row"]
    for r, (kind, wt) in enumerate(rows):
        h = int(avail * wt / total) if r < len(rows) - 1 else SHEET_H - y
        label = labels[r] if r < len(rows) - 1 or len(rows) == 1 else "Bottom row"
        if kind == "char":
            n = names[ci]
            ci += 1
            meta = json.loads((CAST / n / "meta.json").read_text("utf-8"))
            cdir = CAST / n / "cutouts"
            faces = [Image.open(cdir / f) for f in meta.get("faces", [])]
            bodies = [Image.open(cdir / f) for f in meta.get("bodies", [])]
            sheet.paste(grid_row(faces, bodies, SHEET_W, h), (0, y))
            who = roles[ci - 1] if roles and ci - 1 < len(roles) else f"Character {ci}"  # never the name: it gets painted as text
            parts.append(f"{label} ({who}): {meta.get('desc', n)} - shown as two face close-ups "
                         f"(front and three-quarter) and four full-body views (back three-quarter, back, side, front).")
        elif kind == "props":
            pw = (SHEET_W - GUTTER * (len(props) - 1)) // len(props)
            for k, p in enumerate(props):
                im = cutout(Image.open(p))
                b = bbox(im)
                sheet.paste(cell(im.crop(b) if b else im, (pw, h), pad=0.06), (k * (pw + GUTTER), y))
            descs = prop_descs or [f"prop {k + 1}" for k in range(len(props))]
            parts.append(f"{label} (Props, left to right): " + "; ".join(descs) + ".")
        else:
            sheet.paste(cover(Image.open(location), (SHEET_W, h)), (0, y))
            parts.append(f"{label} (Setting): {location_desc or 'the location of the scene'}.")
        y += h + GUTTER
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dest)
    return {"sheet": str(dest), "sheet_desc": " ".join(parts), "characters": names}


def cmd_sheet(a: argparse.Namespace) -> None:
    for n in a.names:
        if not (CAST / n / "panel.png").exists():
            out({"ok": False, "error": f"unknown cast member {n} (create it first)"}, 2)
    out({"ok": True, **compose_sheet(a.names, a.location, a.prop or [], Path(a.out), a.location_desc, a.prop_desc)})


def cmd_list(_: argparse.Namespace) -> None:
    items = []
    for d in sorted(CAST.glob("*/meta.json")):
        m = json.loads(d.read_text("utf-8"))
        items.append({"name": m.get("name"), "desc": m.get("desc"), "panel": str(d.parent / "panel.png")})
    out({"ok": True, "cast": items})


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("name")
    c.add_argument("--image", required=True, help="a clear frame of the character (face visible)")
    c.add_argument("--desc", required=True, help="fixed look, e.g. 'ten-year-old Emirati boy, short black hair, red No.10 jersey'")
    c.add_argument("--who", help="subject phrase for the turnaround prompt (default from desc)")
    c.add_argument("--seed", type=int, default=101)
    c.add_argument("--full", action="store_true", help="casting v2: full-length 9:16 frame, turn + face push-in")
    c.set_defaults(fn=cmd_create)
    pn = sub.add_parser("panel")
    pn.add_argument("name")
    pn.add_argument("--video")
    pn.add_argument("--desc")
    pn.set_defaults(fn=cmd_panel)
    s = sub.add_parser("sheet")
    s.add_argument("names", nargs="*")
    s.add_argument("--out", required=True)
    s.add_argument("--location")
    s.add_argument("--prop", action="append")
    s.add_argument("--location-desc")
    s.add_argument("--prop-desc", action="append")
    s.set_defaults(fn=cmd_sheet)
    ls = sub.add_parser("list")
    ls.set_defaults(fn=cmd_list)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
