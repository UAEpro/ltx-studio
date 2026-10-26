"""Setup page backend: check what this PC has and install the rest from the web app.

Every component has check() -> {ok, detail} and an optional install command that runs as a background job
(films.start_job) so the browser can show live output. Order matters: tools -> engine -> models -> helpers.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import ltxgen  # noqa: E402

ROOT = ltxgen.ROOT
LTXGEN = HERE.parent
REPO = ROOT / "LTX-2"
REPO_PY = REPO / ".venv" / "Scripts" / "python.exe"
MODELS = ROOT / "models" / "ltx-2.5"
QA_MODELS = ROOT / "models" / "qa"
LTX_COMMIT = "e98f6d9"
router = APIRouter()

# (key, title, repo, file in repo, local path under MODELS, GB, level, what it is for)
MODEL_FILES = [
    ("transformer_nvfp4", "Video model (NVFP4, fast)", "Lightricks/LTX-2.5", ltxgen.FILES["transformer_nvfp4"],
     ltxgen.FILES["transformer_nvfp4"], 18.7, "core", "The 22B video+audio model, 4-bit for 16 GB GPUs."),
    ("text_encoder", "Text encoder (Gemma)", "Lightricks/LTX-2.5", ltxgen.FILES["text_encoder"],
     ltxgen.FILES["text_encoder"], 26.3, "core", "Reads your prompts."),
    ("video_vae", "Video decoder", "Lightricks/LTX-2.5", ltxgen.FILES["video_vae"], ltxgen.FILES["video_vae"], 1.5,
     "core", "Turns latents into frames."),
    ("video_vae_conv", "Video decoder (low memory)", "Lightricks/LTX-2.5", ltxgen.FILES["video_vae_conv"],
     ltxgen.FILES["video_vae_conv"], 1.5, "core", "Fallback decoder when VRAM is tight."),
    ("audio_vae", "Audio decoder", "Lightricks/LTX-2.5", ltxgen.FILES["audio_vae"], ltxgen.FILES["audio_vae"], 0.4,
     "core", "Sound, music and dialogue."),
    ("spatial_upsampler", "Upscaler", "Lightricks/LTX-2.5", ltxgen.FILES["spatial_upsampler"],
     ltxgen.FILES["spatial_upsampler"], 1.0, "core", "Second stage for 1280x704 output."),
    ("duration_head", "Duration head", "Lightricks/LTX-2.5", ltxgen.FILES["duration_head"],
     ltxgen.FILES["duration_head"], 0.01, "core", "Picks a clip length from the prompt."),
    ("ingredients_lora", "Character consistency (Ingredients)", "Lightricks/LTX-2.5-22b-IC-LoRA-Ingredients",
     Path(ltxgen.FILES["ingredients_lora"]).name, ltxgen.FILES["ingredients_lora"], 1.3, "consistency",
     "Keeps faces, outfits and props the same in every shot. Gated: accept the licence on Hugging Face first."),
    ("transformer_bf16", "Full-precision model (to build fp8)", "Lightricks/LTX-2.5", ltxgen.FILES["transformer_bf16"],
     ltxgen.FILES["transformer_bf16"], 42.0, "consistency", "Needed once to build the fp8 model below."),
    ("detailing_lora", "Detail upscaler (quality preset)", "Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler",
     Path(ltxgen.FILES["detailing_lora"]).name, ltxgen.FILES["detailing_lora"], 0.3, "quality",
     "Only for the slow 1536x832 'quality' final pass."),
    ("temporal_upsampler", "Temporal upscaler", "Lightricks/LTX-2.5", ltxgen.FILES["temporal_upsampler"],
     ltxgen.FILES["temporal_upsampler"], 0.3, "quality", "Optional smoother motion."),
]
FACE_MODELS = {
    "face_detection_yunet_2023mar.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}


def _run(cmd: list[str], timeout: int = 15) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _gpu() -> dict:
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
    if not out:
        return {"ok": False, "detail": "No NVIDIA GPU / driver found (nvidia-smi missing)."}
    name, mem, drv = [x.strip() for x in out.splitlines()[0].split(",")]
    gb = round(int(mem) / 1024)
    return {"ok": gb >= 12, "detail": f"{name} · {gb} GB · driver {drv}", "warn": gb < 16}


def _ram_disk() -> tuple[dict, dict]:
    import ctypes

    class MS(ctypes.Structure):
        _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                    ("avail", ctypes.c_ulonglong), ("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong),
                    ("c", ctypes.c_ulonglong), ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
    ms = MS()
    ms.l = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
    ram = round(ms.total / 2**30)
    free = round(shutil.disk_usage(ROOT).free / 2**30)
    need = sum(m[5] for m in MODEL_FILES if m[6] in ("core", "consistency") and not (MODELS / m[4]).exists()) + 25
    return ({"ok": ram >= 30, "detail": f"{ram} GB RAM", "warn": ram < 30},
            {"ok": free >= need, "detail": f"{free} GB free on {ROOT.drive or ROOT} (needs ~{round(need)} GB more)"
             if need > 25 else f"{free} GB free on {ROOT.drive or ROOT}"})


def _which(name: str) -> str | None:
    return shutil.which(name)


def _cuda() -> str | None:
    base = Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA")
    vs = sorted((d for d in base.glob("v13.*") if (d / "bin" / "nvcc.exe").exists()), reverse=True) if base.exists() else []
    return str(vs[0]) if vs else None


def _vctools() -> bool:
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / \
        "Installer" / "vswhere.exe"
    if not vswhere.exists():
        return False
    return bool(_run([str(vswhere), "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                      "-property", "installationPath"]))


def _model_state(m) -> dict:
    key, title, repo, rfile, local, gb, level, why = m
    p = MODELS / local
    part = None
    if not p.exists() and _downloading.get(key) and MODELS.exists():  # hf writes <name>.incomplete while downloading
        inc = sorted(MODELS.rglob("*.incomplete"), key=lambda f: f.stat().st_mtime, reverse=True)
        part = inc[0] if inc else None
    have = p.stat().st_size / 1e9 if p.exists() else (part.stat().st_size / 1e9 if part else 0)
    return {"key": key, "title": title, "level": level, "why": why, "gb": gb, "ok": p.exists(),
            "progress": round(min(1.0, have / gb), 3) if gb else 1, "path": str(p)}


_downloading: dict[str, bool] = {}


def status() -> dict:
    ram, disk = _ram_disk()
    winget = bool(_which("winget"))
    groups = [
        {"id": "system", "title": "This computer", "items": [
            {"key": "gpu", "title": "NVIDIA GPU", **_gpu()},
            {"key": "ram", "title": "Memory", **ram},
            {"key": "disk", "title": "Disk space", **disk}]},
        {"id": "tools", "title": "Tools", "items": [
            {"key": "git", "title": "Git", "ok": bool(_which("git")), "detail": _which("git") or "missing",
             "install": winget},
            {"key": "uv", "title": "uv (Python manager)", "ok": bool(_which("uv")), "detail": _which("uv") or "missing",
             "install": winget},
            {"key": "ffmpeg", "title": "FFmpeg", "ok": bool(_which("ffmpeg")), "detail": _which("ffmpeg") or "missing",
             "install": winget},
            {"key": "cuda", "title": "CUDA 13 toolkit", "ok": bool(_cuda()), "detail": _cuda() or
             "missing (needed once to build the fast NVFP4 kernel)", "install": winget},
            {"key": "vctools", "title": "C++ build tools", "ok": _vctools(),
             "detail": "Visual Studio 2022 Build Tools" if _vctools() else "missing (needed once for the kernel build)",
             "install": winget}]},
        {"id": "engine", "title": "LTX engine", "items": [
            {"key": "repo", "title": "LTX-2 code", "ok": (REPO / "pyproject.toml").exists(),
             "detail": str(REPO) if (REPO / "pyproject.toml").exists() else "not downloaded", "install": True},
            {"key": "venv", "title": "Engine Python environment", "ok": REPO_PY.exists(),
             "detail": str(REPO_PY) if REPO_PY.exists() else "not created", "install": True},
            {"key": "kernel", "title": "Fast NVFP4 kernel", "ok": bool(list((REPO / ".venv" / "Lib" / "site-packages")
                                                                           .glob("nvfp4_cpp*.pyd"))),
             "detail": "built" if list((REPO / ".venv" / "Lib" / "site-packages").glob("nvfp4_cpp*.pyd"))
             else "not built (needs CUDA 13 + C++ tools)", "install": True}]},
        {"id": "models", "title": "Models", "items": [_model_state(m) | {"install": True} for m in MODEL_FILES] + [
            {"key": "transformer_fp8", "title": "fp8 model (for consistency renders)", "level": "consistency",
             "ok": (MODELS / ltxgen.FILES["transformer_fp8"]).exists(), "gb": 23.5,
             "why": "Built on this PC from the full-precision model (about 10 minutes).",
             "install": (MODELS / ltxgen.FILES["transformer_bf16"]).exists()}]},
        {"id": "helpers", "title": "Helpers", "items": [
            {"key": "facemodels", "title": "Face check models", "ok": all((QA_MODELS / f).exists() for f in FACE_MODELS),
             "detail": "used by casting and QA", "install": True},
            {"key": "doctor", "title": "Engine self-test", "ok": None, "detail": "runs ltxgen doctor", "install": True},
            {"key": "worker", "title": "Render worker on this PC", "ok": _local_worker_online(),
             "detail": "online" if _local_worker_online() else "not running", "install": True}]},
    ]
    need = [i for g in groups for i in g["items"] if i.get("ok") is False and g["id"] != "system"
            and i.get("level", "core") in ("core", "consistency")]
    return {"groups": groups, "ready": not need and REPO_PY.exists(),
            "missing": [i["title"] for i in need],
            "hf_token": bool(_hf_token()) or (MODELS / ltxgen.FILES["ingredients_lora"]).exists()}


def _local_worker_online() -> bool:
    import socket
    import time
    import server
    host = socket.gethostname().lower()
    for w in server.q("SELECT info, last_seen FROM workers"):
        info = json.loads(w["info"] or "{}")
        if (info.get("hostname") or "").lower() == host and time.time() - (w["last_seen"] or 0) < 90:
            return True
    return False


def _hf_token() -> str | None:
    import server
    return server.CONFIG.get("hf_token") or os.environ.get("HF_TOKEN")


WINGET = {"git": ["Git.Git"], "uv": ["astral-sh.uv"], "ffmpeg": ["Gyan.FFmpeg"],
          "cuda": ["Nvidia.CUDA", "--version", "13.0"],
          "vctools": ["Microsoft.VisualStudio.2022.BuildTools", "--override",
                      "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"]}


def install_cmd(key: str) -> tuple[list[str], str]:
    ps = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
    if key in WINGET:
        wid, *extra = WINGET[key]
        return (["winget", "install", "-e", "--id", wid, "--accept-package-agreements",
                 "--accept-source-agreements", "--disable-interactivity", *extra], f"install {key}")
    if key == "repo":
        return (ps + [f"if (-not (Test-Path '{REPO}\\pyproject.toml')) {{ git clone https://github.com/Lightricks/LTX-2 "
                      f"'{REPO}' }}; git -C '{REPO}' fetch origin; git -C '{REPO}' checkout --quiet {LTX_COMMIT}; "
                      f"git -C '{REPO}' log -1 --oneline"], "download LTX-2 code")
    if key == "venv":
        return (ps + [f"Set-Location '{REPO}'; uv sync --extra natten --inexact; "
                      f"& powershell -NoProfile -ExecutionPolicy Bypass -File '{LTXGEN}\\setup_windows_extras.ps1'"],
                "create engine environment")
    if key == "kernel":
        return (["cmd", "/c", str(LTXGEN / "build_nvfp4.bat")], "build NVFP4 kernel")
    if key == "transformer_fp8":
        return ([str(REPO_PY), str(LTXGEN / "make_fp8_checkpoint.py"), str(MODELS / ltxgen.FILES["transformer_bf16"]),
                 str(MODELS / ltxgen.FILES["transformer_fp8"])], "build fp8 model")
    if key == "facemodels":
        cmds = "; ".join(f"Invoke-WebRequest -UseBasicParsing '{url}' -OutFile '{QA_MODELS / f}'"
                         for f, url in FACE_MODELS.items())
        return (ps + [f"New-Item -ItemType Directory -Force '{QA_MODELS}' | Out-Null; {cmds}; 'done'"],
                "download face models")
    if key == "doctor":
        return ([sys.executable, str(LTXGEN / "ltxgen.py"), "doctor"], "engine self-test")
    if key == "worker":
        import server
        return (ps + [f"Start-Process -WindowStyle Minimized -FilePath '{sys.executable}' -ArgumentList "
                      f"'\"{LTXGEN / 'studio' / 'worker.py'}\"','--server','http://localhost:{server.CONFIG.get('port', 7860)}',"
                      f"'--token','{server.CONFIG['token']}','--worker-id','main-' + $env:COMPUTERNAME.ToLower(),'--no-seed'; "
                      "'worker started'"], "start render worker")
    m = next((m for m in MODEL_FILES if m[0] == key), None)
    if m:
        _, title, repo, rfile, local, gb, level, why = m
        dest = MODELS / Path(local).parent
        py = str(REPO_PY) if REPO_PY.exists() else sys.executable
        code = ("import os,sys;os.environ['HF_HUB_DISABLE_XET']='1';from huggingface_hub import hf_hub_download;"
                f"p=hf_hub_download({repo!r},{rfile!r},local_dir=r'{dest}',token=os.environ.get('HF_TOKEN') or None);"
                "print('{\"ok\": true, \"path\": ' + repr(p).replace(\"'\", '\"') + '}')")
        return ([py, "-c", code], f"download {title}")
    raise HTTPException(400, f"nothing to install for {key}")


@router.get("/api/setup")
def setup_status() -> dict:
    return status()


@router.post("/api/setup/install/{key}")
def setup_install(key: str) -> dict:
    import films
    cmd, title = install_cmd(key)
    if any(m[0] == key for m in MODEL_FILES):
        _downloading[key] = True
        tok = _hf_token()
        if tok:
            os.environ["HF_TOKEN"] = tok
    return films.start_job("setup", None, cmd=cmd, title=title)


@router.post("/api/setup/install-all")
def setup_install_all(body: dict = Body(default={})) -> dict:
    """Queue every missing core/consistency step in order (tools first). Runs one after another."""
    import threading
    import time
    import films
    levels = set(body.get("levels") or ["core", "consistency"])
    st = status()
    order = []
    for g in st["groups"]:
        if g["id"] == "system":
            continue
        for i in g["items"]:
            if i.get("ok") is False and i.get("install") and i.get("level", "core") in levels:
                order.append(i["key"])
    if "transformer_bf16" in order and "transformer_fp8" not in order:
        order.append("transformer_fp8")

    def run():
        for key in order:
            j = setup_install(key)
            while films._jobs[j["id"]]["status"] in ("queued", "running"):
                time.sleep(3)
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "steps": order}


@router.post("/api/settings")
def save_settings(body: dict = Body(...)) -> dict:
    import server
    for k in ("hf_token", "anthropic_api_key", "local_model", "ollama_url"):
        if k in body:
            if body[k]:
                server.CONFIG[k] = body[k]
            else:
                server.CONFIG.pop(k, None)
    if server.CONFIG.get("anthropic_api_key"):
        os.environ["ANTHROPIC_API_KEY"] = server.CONFIG["anthropic_api_key"]
    server.CONFIG_PATH.write_text(json.dumps(server.CONFIG, indent=2), encoding="utf-8")
    return {"ok": True}


@router.get("/api/settings")
def get_settings() -> dict:
    import server
    c = server.CONFIG
    return {"hf_token": bool(c.get("hf_token") or os.environ.get("HF_TOKEN")),
            "anthropic_api_key": bool(c.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")),
            "local_model": c.get("local_model", server.LOCAL_MODEL), "ollama_url": server.OLLAMA_URL,
            "ollama_models": server._ollama_models(), "token": c["token"], "port": c.get("port", 7860)}
