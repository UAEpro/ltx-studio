"""Automated QA for generated shots (used by the weekend benchmark).

  shot VIDEO [--cast NAME ...] [--sheet SHEET.png] [--line "expected dialogue"] [--lang ar]
  film PROJECT_DIR_OR_STORYBOARD ...  (see factory.py)

Metrics per shot (0..1 unless noted):
  face_sim     max cosine similarity of faces in the shot to the cast member's reference faces (SFace;
               >= 0.36 is the same person by OpenCV's threshold), per cast member; None if no face found
  sheet_leak   similarity of the first frames to the reference sheet (high = the sheet is showing in the video)
  motion       mean frame-to-frame change (0 = frozen); black_ratio = share of near-black frames
  loud_lufs / peak_db  integrated loudness / true peak
  speech_cer   character error rate of Whisper transcript vs expected line (lower is better)
Run with D:\\video\\ltxgen\\.venv\\Scripts\\python.exe. Prints one JSON object.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ltxgen  # noqa: E402

QA_MODELS = ltxgen.ROOT / "models" / "qa"
CAST = ltxgen.ROOT / "cast"
_det = _rec = None


def models():
    global _det, _rec
    if _det is None:
        _det = cv2.FaceDetectorYN.create(str(QA_MODELS / "face_detection_yunet_2023mar.onnx"), "", (320, 320), 0.7)
        _rec = cv2.FaceRecognizerSF.create(str(QA_MODELS / "face_recognition_sface_2021dec.onnx"), "")
    return _det, _rec


def faces(img: np.ndarray, min_px: int = 28) -> list[np.ndarray]:
    """SFace embeddings of all faces in a BGR image (faces smaller than min_px are ignored)."""
    det, rec = models()
    h, w = img.shape[:2]
    det.setInputSize((w, h))
    _, found = det.detect(img)
    embs = []
    for f in (found if found is not None else []):
        if min(f[2], f[3]) < min_px:
            continue
        crop = rec.alignCrop(img, f)
        embs.append(rec.feature(crop).flatten())
    return embs


def cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def video_frames(path: Path, n: int = 12) -> list[np.ndarray]:
    cap = cv2.VideoCapture(str(path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    out = []
    for i in np.linspace(0, total - 1, n).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, fr = cap.read()
        if ok:
            out.append(fr)
    cap.release()
    return out


_ref_cache: dict[str, list[np.ndarray]] = {}


def cast_refs(name: str) -> list[np.ndarray]:
    """Reference embeddings for a cast member: face cutouts, source frame, and every 6th turnaround frame with a
    face (multi-angle refs; weekend/exp_faceref.py: consistent 29/48 -> 41/48 on F01+F02, the remaining misses are
    back views). Cached on disk as refs.npy."""
    if name in _ref_cache:
        return _ref_cache[name]
    d = CAST / name
    cache = d / "refs.npy"
    if cache.exists():
        _ref_cache[name] = list(np.load(cache))
        return _ref_cache[name]
    embs = []
    cap = cv2.VideoCapture(str(d / "turnaround" / "video.mp4"))
    i = 0
    while cap.isOpened():
        ok, fr = cap.read()
        if not ok:
            break
        if i % 6 == 0:
            embs += faces(fr, 40)
        i += 1
    cap.release()
    for p in [d / "source.png", *sorted((d / "cutouts").glob("face_*.png"))]:
        if p.exists():
            img = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
            if img is not None and img.shape[2] == 4:  # cutout: put on grey
                a = img[..., 3:4] / 255.0
                img = (img[..., :3] * a + 128 * (1 - a)).astype(np.uint8)
            if img is not None:
                big = cv2.copyMakeBorder(img, 60, 60, 60, 60, cv2.BORDER_CONSTANT, value=(128, 128, 128))
                embs += faces(big)
    if embs:
        np.save(cache, np.stack(embs))
    _ref_cache[name] = embs
    return embs


def leak(frames: list[np.ndarray], sheet: Path) -> float:
    """Correlation between the first frames and the sheet's panel rows (see panel_region)."""
    s = cv2.imread(str(sheet))
    if s is None or not frames:
        return 0.0
    g = cv2.cvtColor(s, cv2.COLOR_BGR2GRAY)
    cut = panel_region(g) / g.shape[0]
    t = cv2.resize(g[: int(cut * g.shape[0])], (96, 40)).astype(np.float32).flatten()
    vals = []
    for fr in frames[:3]:
        gf = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        f = cv2.resize(gf[: int(cut * gf.shape[0])], (96, 40)).astype(np.float32).flatten()
        vals.append(float(np.corrcoef(t, f)[0, 1]))
    return max(0.0, max(vals))


def panel_region(gray: np.ndarray) -> int:
    """Row index where the sheet's panel rows end: the last full-width black gutter (above the location strip).
    A real leak reproduces the panel rows (grey cells + black gutters); the location strip alone looks like a
    normal scene and must not count."""
    dark_rows = np.where((gray < 25).mean(axis=1) > 0.97)[0]
    return int(dark_rows[-1]) if len(dark_rows) and dark_rows[-1] > gray.shape[0] * 0.2 else gray.shape[0]


def leak_seconds(video: Path, sheet: Path, thr: float = 0.5) -> float:
    """How long (from the start) the frames show the reference sheet's panel rows: correlation at 8 fps."""
    s = cv2.imread(str(sheet))
    if s is None:
        return 0.0
    g = cv2.cvtColor(s, cv2.COLOR_BGR2GRAY)
    cut = panel_region(g) / g.shape[0]
    t = cv2.resize(g[: int(cut * g.shape[0])], (96, 40)).astype(np.float32).flatten()
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24
    step, i, last = max(1, int(fps // 8)), 0, -1
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if i % step == 0:
            gf = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            f = cv2.resize(gf[: int(cut * gf.shape[0])], (96, 40)).astype(np.float32).flatten()
            if float(np.corrcoef(t, f)[0, 1]) > thr:
                last = i
            elif i / fps > 0.5 and last < i - 3 * step:
                break
        i += 1
    cap.release()
    return round((last + 1) / fps, 2) if last >= 0 else 0.0


def panel_frame(gray: np.ndarray, sat: np.ndarray | None = None) -> bool:
    """True if a frame shows reference-sheet panels: a horizontal band of light-grey studio cells (~214) split by
    >= 3 near-black vertical gutters. Sheet-independent (F02 s04 leaked with a sheet that was later rebuilt)."""
    h, w = gray.shape
    g = cv2.resize(gray, (960, int(960 * h / w)), interpolation=cv2.INTER_AREA)  # 6 px gutters survive (F04 s16)
    hh = g.shape[0]
    light = (g > 190) & (g < 238)
    if sat is not None:  # sheet cells are NEUTRAL grey; rusty yellow machinery / white trucks with colour cast are not
        sg = cv2.resize(sat, (960, hh), interpolation=cv2.INTER_AREA)
        light &= sg < 30
    # studio cells are FLAT (low local texture); skies/singlets/wood next to dark posts are not (F09 s05/s06 FPs)
    gf = g.astype(np.float32)
    mu = cv2.blur(gf, (7, 7))
    sd = np.sqrt(np.maximum(cv2.blur(gf * gf, (7, 7)) - mu * mu, 0))
    light &= sd < 6
    bh = hh // 8  # thin panel rows (F04 s16 strip was ~1/6 of the frame)
    for y0 in range(0, hh - bh, max(1, hh // 48)):
        band = g[y0:y0 + bh]
        lb = light[y0:y0 + bh]
        if lb.mean() < 0.35:
            continue
        dark_cols = (band < 40).mean(axis=0) > 0.85
        d = np.diff(np.r_[0, dark_cols.astype(int), 0])
        starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        # a sheet gutter is a thin straight black line with flat grey cells on BOTH sides (F09 s06: dark wooden
        # posts next to sky/singlet are not)
        col_light = lb.mean(axis=0)
        gutters = sum(1 for a, b in zip(starts, ends) if b - a <= 14 and a >= 16 and b + 16 < len(col_light)
                      and col_light[a - 16:a - 8].mean() > 0.5 and col_light[b + 8:b + 16].mean() > 0.5)
        if gutters >= 3:
            return True
    return False


def panel_leak_seconds(video: Path) -> float:
    """Seconds (sampled at 4 fps) in which the frame shows reference-sheet panels anywhere in the clip."""
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24
    step, i, hits = max(1, int(fps // 4)), 0, 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if i % step == 0:
            _, fr = cap.retrieve()
            hits += panel_frame(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), cv2.cvtColor(fr, cv2.COLOR_BGR2HSV)[..., 1])
        i += 1
    cap.release()
    return round(hits * step / fps, 2)


def audio_stats(path: Path) -> dict:
    r = subprocess.run([ltxgen.ffmpeg(), "-hide_banner", "-i", str(path), "-af", "ebur128=peak=true", "-vn", "-f",
                        "null", "-"], capture_output=True, text=True)
    i = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    pk = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", r.stderr)
    return {"loud_lufs": float(i[-1]) if i else None, "peak_db": float(pk[-1]) if pk else None}


def romanize(s: str) -> str:
    """Latin-script approximation for comparing a romanized script line (F05 Hindi written as "baarish") with
    Whisper's native-script output; collapses vowel-length spellings (aa/a, ii/ee/i, oo/uu/u)."""
    from unidecode import unidecode
    s = unidecode(s).lower()
    s = re.sub(r"(.)\1+", r"\1", s)
    return s.replace("ee", "i").replace("oo", "u")


_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen " \
        "seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _num_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        return _ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + _num_words(n % 100))
    return str(n)


def spell_numbers(s: str, lang: str | None = None) -> str:
    """Whisper writes "25", scripts write "twenty-five" / "venticinque" (F06 s03, F10 s05: correct lines at CER 0.9)."""
    def words(n: int) -> str:
        if lang and lang != "en":
            try:
                from num2words import num2words
                return num2words(n, lang=lang)
            except Exception:
                pass
        return _num_words(n)
    return re.sub(r"\d+", lambda m: words(int(m.group())), s).replace("-", " ")


def cer(ref: str, hyp: str, lang: str | None = None) -> float:
    if ref.isascii() and hyp.isascii():
        ref, hyp = spell_numbers(ref, lang), spell_numbers(hyp, lang)
    if lang == "ja" or re.search(r"[぀-ヿ]", ref + hyp):
        # Japanese: compare readings (hiragana), since Whisper often picks other kanji/kana for the same sounds (F15)
        try:
            import pykakasi
            kks = pykakasi.kakasi()
            ref, hyp = ("".join(t["hira"] for t in kks.convert(x)) for x in (ref, hyp))
        except ImportError:
            pass
    elif re.search(r"[一-鿿]", ref + hyp):  # traditional script vs Whisper's simplified output (F09)
        try:
            from opencc import OpenCC
            t2s = OpenCC("t2s")
            ref, hyp = t2s.convert(ref), t2s.convert(hyp)
        except ImportError:
            pass
    if ref.isascii() and not hyp.isascii():  # romanized script vs native transcript -> compare in Latin
        # consonant skeletons: Devanagari/Urdu leave short vowels unwritten ("khatam" vs "khtm")
        ref, hyp = (re.sub(r"[aeiouy]", "", romanize(x)) for x in (ref, hyp))
    norm = lambda s: re.sub(r"[\W_]+", "", s.lower())  # noqa: E731
    r, h = norm(ref), norm(hyp)
    if not r:
        return 0.0
    if r in h:  # the line was said in full (actors repeat words: F10 s19 "Nonna, Nonna, Nonna") -> correct
        return 0.0
    d = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hc in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rc != hc))
    return min(1.0, d[len(h)] / len(r))


def shot_qa(video: Path, cast: list[str], sheet: Path | None, line: str | None, lang: str | None,
            transcriber=None) -> dict:
    fr = video_frames(video)
    res: dict = {"video": str(video), "frames_checked": len(fr)}
    gray = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in fr]
    res["motion"] = round(float(np.mean([np.mean(np.abs(a - b)) for a, b in zip(gray, gray[1:])])) / 255, 4) if len(gray) > 1 else 0
    res["black_ratio"] = round(sum(1 for g in gray if g.mean() < 12) / max(1, len(gray)), 3)
    # pillar/letterbox bars (F06 s23 rendered 4:3 inside the 16:9 frame): edge bands near-black in most frames
    def bars(g: np.ndarray) -> bool:
        h, w = g.shape
        pillar = g[:, : w // 12].mean() < 6 and g[:, -w // 12:].mean() < 6
        letter = g[: h // 12].mean() < 6 and g[-h // 12:].mean() < 6
        return (pillar or letter) and g.mean() > 25
    res["bars"] = sum(1 for g in gray if bars(g)) >= max(3, len(gray) // 2)
    # identity is only measurable on faces >= 72 px (1536-wide frames); smaller (wide/underwater) faces gave
    # false "different face" flags in F01 (s12/s13/s16 at 0.17-0.26 while visually the same boy)
    shot_faces = [e for f in fr for e in faces(f, 72)]
    res["faces_found"] = len(shot_faces)
    res["face_sim"] = {}
    for name in cast:
        refs = cast_refs(name)
        if refs and shot_faces:
            res["face_sim"][name] = round(max(cos(a, b) for a in shot_faces for b in refs), 3)
        else:
            res["face_sim"][name] = None
    if sheet:
        res["sheet_leak"] = round(leak(fr, sheet), 3)
        res["leak_seconds"] = leak_seconds(video, sheet)
    res["panel_seconds"] = panel_leak_seconds(video)  # sheet-independent detector (works after sheets are rebuilt)
    res["leak_seconds"] = max(res.get("leak_seconds") or 0.0, res["panel_seconds"])
    res.update(audio_stats(video))
    if line and transcriber:
        heard = transcriber(video, lang)
        res["heard"], res["speech_cer"] = heard, round(cer(line, heard, lang), 3)
    return res


def whisper_transcriber():
    from faster_whisper import WhisperModel
    m = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")

    def run(video: Path, lang: str | None) -> str:
        raw = subprocess.run([ltxgen.ffmpeg(), "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                              "-f", "f32le", "-"], capture_output=True, check=True).stdout
        segs, _ = m.transcribe(np.frombuffer(raw, dtype=np.float32).copy(), language=lang, beam_size=5)
        return " ".join(s.text.strip() for s in segs)
    return run


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("shot")
    s.add_argument("video")
    s.add_argument("--cast", nargs="*", default=[])
    s.add_argument("--sheet")
    s.add_argument("--line")
    s.add_argument("--lang")
    a = p.parse_args()
    tr = whisper_transcriber() if a.line else None
    print(json.dumps(shot_qa(Path(a.video), a.cast, Path(a.sheet) if a.sheet else None, a.line, a.lang, tr),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
