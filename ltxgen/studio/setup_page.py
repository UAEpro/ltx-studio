"""Setup page backend: check what this computer has and install the rest from the web app.

Windows, Linux and macOS. Every component has a check (ok / detail) and, where it can be done without an admin
password, an install command that runs as a background job (films.start_job) so the browser shows live output.
Things that need sudo (apt packages, Homebrew itself) are shown with the exact command to paste in a terminal.

Rendering backends: NVIDIA CUDA on Windows/Linux (4-bit NVFP4 model, fp8 for consistency renders) and Apple Silicon
(MPS) on macOS (full-precision bf16 model - NVFP4/fp8 are NVIDIA-only).

Headless use (e.g. from install.sh / a new worker PC):  python setup_page.py --install-all [--levels core,consistency]
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import ltxgen  # noqa: E402

ROOT = ltxgen.ROOT
LTXGEN = HERE.parent
REPO = ROOT / "LTX-2"
REPO_PY = ltxgen.venv_python(REPO / ".venv")
MODELS = ROOT / "models" / "ltx-2.5"
QA_MODELS = ROOT / "models" / "qa"
LTX_COMMIT = "e98f6d9"
SYSTEM = ltxgen.SYSTEM            # Windows | Linux | Darwin
WIN, MAC, LINUX = SYSTEM == "Windows", SYSTEM == "Darwin", SYSTEM == "Linux"
router = APIRouter()


def _accel() -> str:
    return ltxgen.accelerator()


# (key, title, repo, file in repo, local path under MODELS, GB, level per accelerator, what it is for)
# level: core = needed to render, consistency = needed for consistent characters, quality = optional, None = hidden
def _model_files() -> list[tuple]:
    cuda = _accel() == "cuda"
    F = ltxgen.FILES
    return [
        ("transformer_nvfp4", "Video model (NVFP4, fast)", "Lightricks/LTX-2.5", F["transformer_nvfp4"],
         F["transformer_nvfp4"], 18.7, "core" if cuda else None, "The 22B video+audio model, 4-bit for 16 GB GPUs."),
        ("transformer_bf16", "Video model (full precision)" if not cuda else "Full-precision model (to build fp8)",
         "Lightricks/LTX-2.5", F["transformer_bf16"], F["transformer_bf16"], 42.0,
         "core" if not cuda else "consistency",
         "The 22B video+audio model. Apple Silicon runs it in bf16 (needs a Mac with 64 GB+ memory)."
         if not cuda else "Needed once to build the fp8 model below."),
        ("text_encoder", "Text encoder (Gemma)", "Lightricks/LTX-2.5", F["text_encoder"], F["text_encoder"], 26.3,
         "core", "Reads your prompts."),
        ("video_vae", "Video decoder", "Lightricks/LTX-2.5", F["video_vae"], F["video_vae"], 1.5, "core",
         "Turns latents into frames."),
        ("video_vae_conv", "Video decoder (low memory)", "Lightricks/LTX-2.5", F["video_vae_conv"],
         F["video_vae_conv"], 1.5, "core", "Fallback decoder when memory is tight."),
        ("audio_vae", "Audio decoder", "Lightricks/LTX-2.5", F["audio_vae"], F["audio_vae"], 0.4, "core",
         "Sound, music and dialogue."),
        ("spatial_upsampler", "Upscaler", "Lightricks/LTX-2.5", F["spatial_upsampler"], F["spatial_upsampler"], 1.0,
         "core", "Second stage for 1280x704 output."),
        ("duration_head", "Duration head", "Lightricks/LTX-2.5", F["duration_head"], F["duration_head"], 0.01,
         "core", "Picks a clip length from the prompt."),
        ("ingredients_lora", "Character consistency (Ingredients)", "Lightricks/LTX-2.5-22b-IC-LoRA-Ingredients",
         Path(F["ingredients_lora"]).name, F["ingredients_lora"], 1.3, "consistency",
         "Keeps faces, outfits and props the same in every shot. Gated: accept the licence on Hugging Face first."),
        ("detailing_lora", "Detail upscaler (quality preset)", "Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler",
         Path(F["detailing_lora"]).name, F["detailing_lora"], 0.3, "quality",
         "Only for the slow 1536x832 'quality' final pass."),
        ("temporal_upsampler", "Temporal upscaler", "Lightricks/LTX-2.5", F["temporal_upsampler"],
         F["temporal_upsampler"], 0.3, "quality", "Optional smoother motion."),
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


# --------------------------------------------------------------------------- system checks
def _ram_gb() -> int:
    try:
        if WIN:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong),
                            ("c", ctypes.c_ulonglong), ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
            ms = MS()
            ms.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return round(ms.total / 2**30)
        if MAC:
            return round(int(_run(["sysctl", "-n", "hw.memsize"]) or 0) / 2**30)
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 2**20)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _gpu() -> dict:
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
    if out:
        name, mem, drv = [x.strip() for x in out.splitlines()[0].split(",")]
        gb = round(int(float(mem)) / 1024)
        return {"ok": gb >= 12, "detail": f"{name} · {gb} GB · driver {drv}", "warn": gb < 16}
    if MAC and platform.machine() == "arm64":
        chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "Apple Silicon"
        ram = _ram_gb()
        return {"ok": ram >= 64, "warn": ram < 96,
                "detail": f"{chip} · {ram} GB unified memory" + ("" if ram >= 64 else " - 64 GB+ needed to render")}
    if MAC:
        return {"ok": False, "detail": "Intel Mac - rendering needs Apple Silicon (M1 or newer) or an NVIDIA PC. "
                                       "This Mac can still run the studio and send shots to other PCs."}
    return {"ok": False, "detail": "No NVIDIA GPU / driver found (nvidia-smi missing). This computer can still run "
                                   "the studio and send shots to render workers on other PCs."}


def _ram_disk() -> tuple[dict, dict]:
    ram = _ram_gb()
    free = round(shutil.disk_usage(ROOT).free / 2**30)
    need = sum(m[5] for m in _model_files() if m[6] in ("core", "consistency") and not (MODELS / m[4]).exists()) + 25
    where = ROOT.drive or str(ROOT.anchor or ROOT)
    ram_need = 64 if _accel() == "mps" else 30
    return ({"ok": ram >= ram_need, "detail": f"{ram} GB RAM" + ("" if ram >= ram_need else f" ({ram_need} GB+ recommended)"),
             "warn": ram < ram_need},
            {"ok": free >= need, "detail": f"{free} GB free on {where} (needs ~{round(need)} GB more)"
             if need > 25 else f"{free} GB free on {where}"})


def _which(name: str) -> str | None:
    return shutil.which(name)


def _cuda_win() -> str | None:
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


_kernel_cache: dict = {"t": 0, "ok": False}


def _kernel_ok() -> bool:
    if not REPO_PY.exists():
        return False
    if time.time() - _kernel_cache["t"] > 120:
        _kernel_cache.update(t=time.time(), ok=_run([str(REPO_PY), "-c", "import importlib.util as u;"
                                                     "print(bool(u.find_spec('nvfp4_cpp')))"], timeout=60) == "True")
    return _kernel_cache["ok"]


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


def _tool(key: str, title: str, ok: bool, detail_ok: str, manual: str | None, auto: bool) -> dict:
    """auto: can install from the web app; manual: command the user runs (needs an admin password)."""
    d = {"key": key, "title": title, "ok": ok,
         "detail": detail_ok if ok else ("missing - needs your admin password, run this in a terminal:" if manual and not auto else "missing"),
         "install": auto and not ok}
    if manual and not ok and not auto:
        d["manual"] = manual
    return d


def _tools() -> list[dict]:
    git, uv, ff = _which("git"), _which("uv"), _which("ffmpeg")
    if WIN:
        wg = bool(_which("winget"))
        return [
            _tool("git", "Git", bool(git), git or "", None, wg),
            _tool("uv", "uv (Python manager)", bool(uv), uv or "", None, wg),
            _tool("ffmpeg", "FFmpeg", bool(ff), ff or "", None, wg),
            {**_tool("cuda", "CUDA 13 toolkit", bool(_cuda_win()), _cuda_win() or "", None, wg),
             **({} if _cuda_win() else {"detail": "missing (needed once to build the fast NVFP4 kernel)"})},
            {**_tool("vctools", "C++ build tools", _vctools(), "Visual Studio 2022 Build Tools", None, wg),
             **({} if _vctools() else {"detail": "missing (needed once for the kernel build)"})},
        ]
    if MAC:
        brew = _which("brew")
        items = [_tool("brew", "Homebrew", bool(brew), brew or "",
                       '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
                       False)]
        items += [_tool("git", "Git", bool(git), git or "", "xcode-select --install", bool(brew)),
                  _tool("uv", "uv (Python manager)", bool(uv), uv or "", None, True),
                  _tool("ffmpeg", "FFmpeg", bool(ff), ff or "", "brew install ffmpeg", bool(brew))]
        return items
    apt = "sudo apt install -y" if _which("apt") else ("sudo dnf install -y" if _which("dnf") else "install with your package manager:")
    cc = _which("g++") or _which("c++")
    items = [_tool("git", "Git", bool(git), git or "", f"{apt} git", False),
             _tool("uv", "uv (Python manager)", bool(uv), uv or "", None, True),
             _tool("ffmpeg", "FFmpeg", bool(ff), ff or "", f"{apt} ffmpeg", False)]
    if _accel() == "cuda":
        items.append({**_tool("cc", "C++ compiler", bool(cc), cc or "",
                              f"{apt} build-essential" if "apt" in apt else f"{apt} gcc-c++", False),
                      **({} if cc else {"detail": "missing (needed once to build the fast NVFP4 kernel) - run this in a terminal:"})})
    return items


def status() -> dict:
    ram, disk = _ram_disk()
    accel = _accel()
    engine = [
        {"key": "repo", "title": "LTX-2 code", "ok": (REPO / "pyproject.toml").exists(),
         "detail": str(REPO) if (REPO / "pyproject.toml").exists() else "not downloaded", "install": True},
        {"key": "venv", "title": "Engine Python environment", "ok": REPO_PY.exists(),
         "detail": str(REPO_PY) if REPO_PY.exists() else "not created", "install": True}]
    if accel == "cuda":
        k = _kernel_ok()
        engine.append({"key": "kernel", "title": "Fast NVFP4 kernel", "ok": k, "install": True,
                       "detail": "built" if k else "not built" + (" (needs CUDA 13 + C++ tools)" if WIN else "")})
    models = [_model_state(m) | {"install": True} for m in _model_files() if m[6]]
    if accel == "cuda":
        models.append({"key": "transformer_fp8", "title": "fp8 model (for consistency renders)", "level": "consistency",
                       "ok": (MODELS / ltxgen.FILES["transformer_fp8"]).exists(), "gb": 23.5,
                       "why": "Built on this PC from the full-precision model (about 10 minutes).",
                       "install": (MODELS / ltxgen.FILES["transformer_bf16"]).exists()})
    groups = [
        {"id": "system", "title": "This computer", "items": [
            {"key": "gpu", "title": "Apple Silicon GPU" if accel == "mps" else "NVIDIA GPU", **_gpu()},
            {"key": "ram", "title": "Memory", **ram},
            {"key": "disk", "title": "Disk space", **disk}]},
        {"id": "tools", "title": "Tools", "items": _tools()},
        {"id": "engine", "title": "LTX engine", "items": engine},
        {"id": "models", "title": "Models", "items": models},
        {"id": "helpers", "title": "Helpers", "items": [
            {"key": "facemodels", "title": "Face check models", "ok": all((QA_MODELS / f).exists() for f in FACE_MODELS),
             "detail": "used by casting and QA", "install": True},
            {"key": "doctor", "title": "Engine self-test", "ok": None, "detail": "runs ltxgen doctor", "install": True},
            {"key": "worker", "title": "Render worker on this computer", "ok": _local_worker_online(),
             "detail": "online" if _local_worker_online() else "not running", "install": True}]},
    ]
    need = [i for g in groups for i in g["items"] if i.get("ok") is False and g["id"] != "system"
            and i.get("level", "core") in ("core", "consistency")]
    return {"groups": groups, "ready": not need and REPO_PY.exists(), "system": SYSTEM, "accelerator": accel,
            "missing": [i["title"] for i in need],
            "hf_token": bool(_hf_token()) or (MODELS / ltxgen.FILES["ingredients_lora"]).exists()}


def _local_worker_online() -> bool:
    import socket
    try:
        import server
    except Exception:  # noqa: BLE001  (headless CLI use)
        return False
    host = socket.gethostname().lower()
    for w in server.q("SELECT info, last_seen FROM workers"):
        info = json.loads(w["info"] or "{}")
        if (info.get("hostname") or "").lower() == host and time.time() - (w["last_seen"] or 0) < 90:
            return True
    return False


def _hf_token() -> str | None:
    try:
        import server
        return server.CONFIG.get("hf_token") or os.environ.get("HF_TOKEN")
    except Exception:  # noqa: BLE001
        return os.environ.get("HF_TOKEN")


# --------------------------------------------------------------------------- installers
WINGET = {"git": ["Git.Git"], "uv": ["astral-sh.uv"], "ffmpeg": ["Gyan.FFmpeg"],
          "cuda": ["Nvidia.CUDA", "--version", "13.0"],
          "vctools": ["Microsoft.VisualStudio.2022.BuildTools", "--override",
                      "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"]}


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def install_cmd(key: str) -> tuple[list[str], str]:
    uv = _which("uv") or "uv"
    if WIN and key in WINGET:
        wid, *extra = WINGET[key]
        return (["winget", "install", "-e", "--id", wid, "--accept-package-agreements",
                 "--accept-source-agreements", "--disable-interactivity", *extra], f"install {key}")
    if MAC and key in ("git", "ffmpeg"):
        return ([_which("brew") or "brew", "install", key], f"install {key}")
    if key == "uv" and not WIN:
        return (["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"], "install uv")
    if key == "repo":
        return (_py("import subprocess as s,pathlib as p,sys;r=p.Path(sys.argv[1]);"
                    "(r/'pyproject.toml').exists() or s.run(['git','clone','https://github.com/Lightricks/LTX-2',str(r)],check=True);"
                    "s.run(['git','-C',str(r),'fetch','origin'],check=True);"
                    f"s.run(['git','-C',str(r),'checkout','--quiet','{LTX_COMMIT}'],check=True);"
                    "s.run(['git','-C',str(r),'log','-1','--oneline'])") + [str(REPO)], "download LTX-2 code")
    if key == "venv":
        extra = ["--extra", "natten"] if _accel() == "cuda" else []
        if WIN:
            return (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                     f"Set-Location '{REPO}'; uv sync --extra natten --inexact; "
                     f"& powershell -NoProfile -ExecutionPolicy Bypass -File '{LTXGEN}\\setup_windows_extras.ps1'"],
                    "create engine environment")
        return ([uv, "--directory", str(REPO), "sync", *extra, "--inexact"], "create engine environment")
    if key == "kernel":
        if WIN:
            return (["cmd", "/c", str(LTXGEN / "build_nvfp4.bat")], "build NVFP4 kernel")
        _kernel_cache["t"] = 0
        return ([uv, "--directory", str(REPO), "sync", "--extra", "natten", "--inexact",
                 "--reinstall-package", "ltx-kernels"], "build NVFP4 kernel")
    if key == "transformer_fp8":
        return ([str(REPO_PY), str(LTXGEN / "make_fp8_checkpoint.py"), str(MODELS / ltxgen.FILES["transformer_bf16"]),
                 str(MODELS / ltxgen.FILES["transformer_fp8"])], "build fp8 model")
    if key == "facemodels":
        pairs = json.dumps({str(QA_MODELS / f): url for f, url in FACE_MODELS.items()})
        return (_py("import json,sys,pathlib,urllib.request as u\n"
                    "for dst,url in json.loads(sys.argv[1]).items():\n"
                    "    pathlib.Path(dst).parent.mkdir(parents=True,exist_ok=True); u.urlretrieve(url,dst); print(dst)")
                + [pairs], "download face models")
    if key == "doctor":
        return ([sys.executable, str(LTXGEN / "ltxgen.py"), "doctor"], "engine self-test")
    m = next((m for m in _model_files() if m[0] == key), None)
    if m:
        _, title, repo, rfile, local, gb, level, why = m
        dest = MODELS / Path(local).parent
        py = str(REPO_PY) if REPO_PY.exists() else sys.executable
        code = ("import os,sys;os.environ['HF_HUB_DISABLE_XET']='1';from huggingface_hub import hf_hub_download;"
                "p=hf_hub_download(sys.argv[1],sys.argv[2],local_dir=sys.argv[3],token=os.environ.get('HF_TOKEN') or None);"
                "print('{\"ok\": true}')")
        return ([py, "-c", code, repo, rfile, str(dest)], f"download {title}")
    raise HTTPException(400, f"nothing to install for {key}")


def start_worker() -> dict:
    """Start the render worker on this computer as a detached process (survives the studio restarting)."""
    import server
    log = ROOT / "studio" / "worker.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(HERE / "worker.py"), "--server", f"http://localhost:{server.CONFIG.get('port', 7860)}",
           "--token", server.CONFIG["token"], "--no-seed"]
    kw: dict = {"stdout": open(log, "ab"), "stderr": subprocess.STDOUT, "cwd": str(ROOT)}
    if WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)
    return {"ok": True, "log": str(log)}


@router.get("/api/setup")
def setup_status() -> dict:
    return status()


@router.post("/api/setup/install/{key}")
def setup_install(key: str) -> dict:
    import films
    if key == "worker":
        start_worker()
        return films.start_job("setup", None, cmd=_py("print('render worker started - see studio/worker.log')"),
                               title="start render worker")
    cmd, title = install_cmd(key)
    if any(m[0] == key for m in _model_files()):
        _downloading[key] = True
        tok = _hf_token()
        if tok:
            os.environ["HF_TOKEN"] = tok
    return films.start_job("setup", None, cmd=cmd, title=title)


def _plan(levels: set[str]) -> list[str]:
    order = []
    for g in status()["groups"]:
        if g["id"] == "system":
            continue
        for i in g["items"]:
            if i.get("ok") is False and i.get("install") and i.get("level", "core") in levels:
                order.append(i["key"])
    if "transformer_bf16" in order and _accel() == "cuda" and "transformer_fp8" not in order:
        order.append("transformer_fp8")
    return order


@router.post("/api/setup/install-all")
def setup_install_all(body: dict = Body(default={})) -> dict:
    """Queue every missing installable step in order (tools first) and run them one after another."""
    import threading
    import films
    order = _plan(set(body.get("levels") or ["core", "consistency"]))

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
            "ollama_models": server._ollama_models(), "token": c["token"], "port": c.get("port", 7860),
            "lan_url": f"http://{server._lan_ip()}:{c.get('port', 7860)}"}


# --------------------------------------------------------------------------- headless CLI
def _cli() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="LTX Studio setup without the browser")
    ap.add_argument("--check", action="store_true", help="print what is missing")
    ap.add_argument("--install-all", action="store_true", help="install every missing step that needs no sudo")
    ap.add_argument("--levels", default="core,consistency", help="core,consistency,quality")
    a = ap.parse_args()
    st = status()
    for g in st["groups"]:
        print(f"\n{g['title']}")
        for i in g["items"]:
            mark = {True: "ok ", False: "-- ", None: "?? "}[i.get("ok")]
            print(f"  {mark} {i['title']}: {i.get('detail') or i.get('why') or ''}")
    if not a.install_all:
        return
    for key in _plan(set(a.levels.split(","))):
        if key == "worker":
            continue
        cmd, title = install_cmd(key)
        print(f"\n==> {title}", flush=True)
        r = subprocess.run(cmd, cwd=str(ROOT), env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if r.returncode:
            print(f"    failed ({r.returncode}) - fix it and re-run", flush=True)
            sys.exit(r.returncode)
    manual = [i for g in status()["groups"] for i in g["items"] if i.get("manual")]
    for i in manual:
        print(f"\nStill needed (run yourself): {i['manual']}")


if __name__ == "__main__":
    _cli()
