"""Transcribe the speech in generated clips (QA for dialogue). Runs on CPU with faster-whisper.

Usage: D:\\video\\ltxgen\\.venv\\Scripts\\python.exe transcribe.py VIDEO [VIDEO...] [--lang ar] [--model large-v3-turbo]
Prints one JSON object: {video: {language, probability, text}}.
"""
import argparse
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

from faster_whisper import WhisperModel

ap = argparse.ArgumentParser()
ap.add_argument("videos", nargs="+")
ap.add_argument("--lang", default=None, help="force language (e.g. ar, en); default auto-detect")
ap.add_argument("--model", default="large-v3-turbo")
a = ap.parse_args()

def load_audio(path: str) -> "numpy.ndarray":
    """16 kHz mono float32 via ffmpeg (faster-whisper's PyAV decoder breaks on newer av versions)."""
    import shutil
    import subprocess

    import numpy as np
    raw = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


model = WhisperModel(a.model, device="cpu", compute_type="int8")
out = {}
for v in a.videos:
    segs, info = model.transcribe(load_audio(v), language=a.lang, vad_filter=False, beam_size=5)
    out[v] = {"language": info.language, "probability": round(info.language_probability, 2),
              "text": " ".join(s.text.strip() for s in segs)}
print(json.dumps(out, ensure_ascii=False, indent=2))
