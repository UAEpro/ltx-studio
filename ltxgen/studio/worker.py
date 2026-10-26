"""LTX Studio worker: pulls shot tasks from the studio server and renders them with ltxgen.py.

Stdlib only (any Python >= 3.10). Implements the worker side of studio/PROTOCOL.md (v1):
register -> loop { claim -> download inputs -> `ltxgen.py generate` in a subprocess
(heartbeat every 20 s with live progress; cancel=true kills the process tree) -> complete | fail }.
One task at a time. Server outages are retried with backoff; the worker never exits on its own
(except with --once, which prints one JSON summary on stdout).

  python worker.py --server http://MAINPC:7860 --token SECRET
  python worker.py --server http://MAINPC:7860 --token SECRET --dry-run --once   # protocol test, no GPU

--dry-run replaces ltxgen with a fake generator (tiny ffmpeg test clip, same files and JSON shape).
Seeding (v1.1): a low-priority HTTP server (--seed-port 7861) serves manifest files to joining PCs;
default: on once every manifest file is present locally (never on the server's own PC), --seed forces it on.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

VERSION = "1"
PRESETS = ("draft", "standard", "quality")
HERE = Path(__file__).resolve().parent
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
STEP = re.compile(r"\d+\s*/\s*\d+|\d+%")
log = logging.getLogger("worker")

# Fallback if ltxgen.py cannot be imported (must match ltxgen.FILES).
FILES = {
    "transformer_nvfp4": "diffusion_models/ltx-2.5-22b-distilled-transformer-nvfp4.safetensors",
    "transformer_bf16": "diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors",
    "text_encoder": "text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors",
    "video_vae": "vae/ltx-2.5-video-vae-bf16.safetensors",
    "video_vae_conv": "vae/ltx-2.5-video-vae-conv-bf16.safetensors",
    "audio_vae": "vae/ltx-2.5-audio-vae-bf16.safetensors",
    "duration_head": "model_patches/ltx-2.5-duration-head-bf16.safetensors",
    "spatial_upsampler": "latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
    "detailing_lora": "loras/ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors",
}


# --------------------------------------------------------------------------- HTTP
class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


class Api:
    """Tiny JSON/multipart client. Network errors raise OSError; HTTP errors raise ApiError."""

    def __init__(self, server: str, token: str):
        self.base = server.rstrip("/")
        self.token = token
        # LAN server: never route through a system proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def url(self, path: str) -> str:
        return path if path.startswith(("http://", "https://")) else self.base + "/" + path.lstrip("/")

    def call(self, method: str, path: str, body: Any = None, *, data: Any = None, headers: dict | None = None,
             timeout: float = 30, length: int | None = None) -> tuple[int, Any]:
        h = {"X-LTX-Token": self.token, "User-Agent": f"ltx-studio-worker/{VERSION}", **(headers or {})}
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if length is not None:
            h["Content-Length"] = str(length)
        req = urllib.request.Request(self.url(path), data=data, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                raw = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode("utf-8", "replace")) from None
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, raw.decode("utf-8", "replace")

    def download(self, path: str, dest: Path, timeout: float = 120) -> int:
        req = urllib.request.Request(self.url(path), headers={"X-LTX-Token": self.token})
        try:
            with self.opener.open(req, timeout=timeout) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode("utf-8", "replace")) from None
        return dest.stat().st_size


def multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[str, int, Iterator[bytes]]:
    """Streamed multipart/form-data body: (content_type, content_length, chunk iterator)."""
    boundary = "----ltxstudio" + uuid.uuid4().hex
    parts: list[bytes | Path] = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n'
                     f"Content-Type: text/plain; charset=utf-8\r\n\r\n".encode() + v.encode() + b"\r\n")
    ctypes_ = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".txt": "text/plain"}
    for k, p in files.items():
        ct = ctypes_.get(p.suffix.lower(), "application/octet-stream")
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{p.name}"\r\n'
                     f"Content-Type: {ct}\r\n\r\n".encode())
        parts.append(p)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    length = sum(p.stat().st_size if isinstance(p, Path) else len(p) for p in parts)

    def gen() -> Iterator[bytes]:
        for p in parts:
            if isinstance(p, Path):
                with open(p, "rb") as f:
                    while chunk := f.read(1 << 20):
                        yield chunk
            else:
                yield p
    return f"multipart/form-data; boundary={boundary}", length, gen()


# --------------------------------------------------------------------------- machine info
def gpu_info() -> tuple[str | None, int | None]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        if sys.platform == "darwin" and os.uname().machine == "arm64":  # Apple Silicon: unified memory
            try:
                chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                                      text=True, timeout=10).stdout.strip() or "Apple Silicon"
                mem = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                                         timeout=10).stdout.strip())
                return chip, round(mem / 2**30)
            except Exception:
                return "Apple Silicon", None
        return None, None
    try:
        r = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=20)
        name, mib = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")[:2]]
        return name, round(int(float(mib)) / 1024)
    except Exception:
        return None, None


def default_worker_id(gpu: str | None) -> str:
    host = re.sub(r"[^a-z0-9-]+", "-", socket.gethostname().lower()).strip("-") or "pc"
    short = "nogpu"
    if gpu:
        m = re.findall(r"\d{3,4}\w*", gpu)
        short = (m[-1] if m else gpu.split()[-1]).lower()
    return f"{host}-{short}"


def load_ltxgen(path: Path) -> Any | None:
    """Import ltxgen.py (stdlib, side-effect free) to share FILES / load_config with the renderer."""
    try:
        spec = importlib.util.spec_from_file_location("ltxgen", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        log.warning("could not import %s (%s); using built-in file list", path, e)
        return None


class Models:
    """Which model files exist -> runnable presets and the ltxgen flags needed to use them."""

    def __init__(self, root: Path, ltxgen: Any | None):
        self.dir = root / "models" / "ltx-2.5"
        self.files = getattr(ltxgen, "FILES", FILES)
        self.cfg = ltxgen.load_config() if ltxgen and hasattr(ltxgen, "load_config") else {}
        self.venv = (root / "LTX-2" / ".venv" / "Scripts" / "python.exe" if os.name == "nt"
                     else root / "LTX-2" / ".venv" / "bin" / "python")

    def has(self, key: str) -> bool:
        p = self.dir / self.files[key]
        return p.is_file() and p.stat().st_size > 1_000_000

    def presets(self) -> list[str]:
        if not self.venv.exists():
            log.warning("LTX-2 venv missing (%s): advertising no presets", self.venv)
            return []
        base = ((self.has("transformer_nvfp4") or self.has("transformer_bf16")) and self.has("text_encoder")
                and (self.has("video_vae") or self.has("video_vae_conv")) and self.has("audio_vae")
                and self.has("spatial_upsampler"))
        if not base:
            return []
        quality = self.has("transformer_bf16") and self.has("detailing_lora")
        ingredients = ((self.has("transformer_bf16") or ("transformer_fp8" in self.files and self.has("transformer_fp8")))
                       and "ingredients_lora" in self.files and self.has("ingredients_lora"))
        return ["draft", "standard"] + (["quality"] if quality else []) + (["ingredients"] if ingredients else [])

    def extra_args(self, preset: str) -> list[str]:
        """Fallback flags when the configured default file is absent (e.g. bf16-only PC)."""
        args: list[str] = []
        quant = self.cfg.get("quantization", "nvfp4-prequant")
        if preset != "quality" and quant == "nvfp4-prequant" and not self.has("transformer_nvfp4") \
                and self.has("transformer_bf16"):
            args += ["--quantization", "fp8-cast", "--transformer", "transformer_bf16"]
        vae = self.cfg.get("video_vae", "video_vae")
        other = "video_vae_conv" if vae == "video_vae" else "video_vae"
        if not self.has(vae) and self.has(other):
            args += ["--vae", other]
        return args


# --------------------------------------------------------------------------- process helpers
def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:  # the render runs in its own session (start_new_session) -> kill the whole group
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            proc.kill()


def tail_lines(path: Path, n: int = 25, nbytes: int = 65536) -> list[str]:
    """Last n non-empty lines of a (possibly growing, tqdm-\\r-updated) log file."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, f.seek(0, 2) - nbytes))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = [ANSI.sub("", s).strip() for s in re.split(r"[\r\n]+", text)]
    return [s for s in lines if s][-n:]


def progress_of(path: Path) -> str | None:
    """Last meaningful log line, preferring step counters / percentages."""
    lines = tail_lines(path, 40)
    for s in reversed(lines):
        if STEP.search(s):
            return s[-200:]
    return lines[-1][-200:] if lines else None


def keep_awake(on: bool) -> None:
    """Stop Windows from sleeping while the worker runs (display may still turn off)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        flags = 0x80000000 | (0x00000001 if on else 0)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass



# --------------------------------------------------------------------------- seeding (PROTOCOL v1.1)
def lan_ip_towards(host: str, port: int = 80) -> str:
    """Local address this PC uses to reach `host` (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, port))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def background_thread() -> None:
    """Lower CPU + I/O priority of the calling thread so seeding never slows a render (Windows)."""
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetThreadPriority(k.GetCurrentThread(), 0x00010000)  # THREAD_MODE_BACKGROUND_BEGIN
        except Exception:
            pass


class SeedHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ltx-seeder/1"
    seeder: "Seeder"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("seed %s - " + fmt, self.client_address[0], *args)

    def reply(self, code: int, msg: str, headers: dict | None = None) -> None:
        body = json.dumps({"detail": msg}).encode()
        self.send_response(code)
        for k, v in {"Content-Type": "application/json", "Content-Length": str(len(body)), **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        sd = self.seeder
        if self.headers.get("X-LTX-Token") != sd.token:
            return self.reply(401, "bad or missing X-LTX-Token")
        raw = urllib.parse.urlsplit(self.path).path
        if not raw.startswith("/dist/file/"):
            return self.reply(404, "not found")
        rel = urllib.parse.unquote(raw[len("/dist/file/"):])
        parts = rel.split("/")
        if not rel or "\\" in rel or ":" in rel or any(x in ("", ".", "..") for x in parts):
            return self.reply(400, "bad path")
        size = sd.files.get(rel)
        local = sd.root.joinpath(*parts)
        if size is None or not local.is_file() or local.stat().st_size != size:
            return self.reply(404, "not available on this seeder")
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng:
            m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", rng)
            if not m or (not m[1] and not m[2]):
                return self.reply(416, "bad range", {"Content-Range": f"bytes */{size}"})
            if m[1]:
                start, end = int(m[1]), min(int(m[2]) if m[2] else size - 1, size - 1)
            else:
                start, end = max(0, size - int(m[2])), size - 1
            if start > end or start >= size:
                return self.reply(416, "range not satisfiable", {"Content-Range": f"bytes */{size}"})
        if not sd.slots.acquire(blocking=False):
            return self.reply(503, "seeder busy", {"Retry-After": "2"})
        try:
            background_thread()
            length = max(end - start + 1, 0)
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(local, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            sd.served += length
        except (ConnectionError, OSError):
            self.close_connection = True
        finally:
            sd.slots.release()


class Seeder:
    """Serves manifest files this PC has (same size) to joining PCs; reports seed_version when complete."""

    def __init__(self, api: Api, root: Path, token: str, port: int, mode: bool | None, max_uploads: int = 4):
        self.api, self.root, self.token, self.port, self.mode = api, root, token, port, mode
        self.files: dict[str, int] = {}
        self.version: str | None = None
        self.seed_version: str | None = None
        self.max_uploads = max_uploads
        self.slots = threading.BoundedSemaphore(max_uploads)
        self.httpd: ThreadingHTTPServer | None = None
        self.served = 0
        self.checked = 0.0
        u = urllib.parse.urlsplit(api.base)
        host = u.hostname or "localhost"
        self.ip = lan_ip_towards(host, u.port or 80)
        try:  # the main PC's own worker need not seed: the server already serves every file
            addr = socket.gethostbyname(host)
            self.same_host = addr == self.ip or addr.startswith("127.")
        except OSError:
            self.same_host = False

    @property
    def url(self) -> str | None:
        return f"http://{self.ip}:{self.port}/dist/file/" if self.httpd else None

    def refresh(self) -> bool:
        """Re-read the manifest; recheck local sizes on version change or every 5 min. True if state changed."""
        try:
            _, m = self.api.call("GET", "/api/dist/manifest", timeout=120)
        except ApiError as e:
            if e.status != 503:
                log.debug("seeding: manifest unavailable (%s)", e)
            return False
        except OSError:
            return False
        if not isinstance(m, dict) or not isinstance(m.get("files"), list):
            return False
        before = (self.seed_version, self.url)
        if m.get("version") != self.version or time.time() - self.checked > 300:
            self.version, self.checked = m.get("version"), time.time()
            self.files = {f["path"]: int(f["size"]) for f in m["files"]}
            missing = [p for p, n in self.files.items() if not self._has(p, n)]
            self.seed_version = None if missing else self.version
            if missing:
                log.info("seeding: %d/%d manifest files present here (e.g. missing %s)",
                         len(self.files) - len(missing), len(self.files), missing[0])
        want = self.mode is True or (self.mode is None and self.seed_version is not None and not self.same_host)
        if want and not self.httpd:
            self.start()
        return (self.seed_version, self.url) != before

    def _has(self, rel: str, size: int) -> bool:
        try:
            return self.root.joinpath(*rel.split("/")).stat().st_size == size
        except OSError:
            return False

    def start(self) -> None:
        handler = type("Handler", (SeedHandler,), {"seeder": self})
        try:
            self.httpd = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        except OSError as e:
            log.error("seeding: cannot listen on port %d (%s)", self.port, e)
            return
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="seeder").start()
        log.info("seeding on %s (version %s, max %d uploads)", self.url, self.seed_version, self.max_uploads)


# --------------------------------------------------------------------------- worker
class Worker:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.root = Path(a.root)
        self.work = self.root / "studio_worker"
        self.api = Api(a.server, a.token)
        ltxgen_path = Path(a.ltxgen)
        self.ltxgen_path = ltxgen_path
        self.models = Models(self.root, load_ltxgen(ltxgen_path) if ltxgen_path.exists() else None)
        self.gpu, self.vram = gpu_info()
        self.worker_id = a.worker_id or default_worker_id(self.gpu)
        self.presets = self.detect_presets()
        self.poll_s = 5.0
        self.lock = threading.RLock()
        self.status, self.task_id, self.progress = "idle", None, None
        self.proc: subprocess.Popen | None = None
        self.log_path: Path | None = None
        self.task_t0 = 0.0
        self.cancel = threading.Event()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.registered = threading.Event()
        self.seeder = None if a.seed is False else Seeder(self.api, self.root, a.token, a.seed_port, a.seed)

    # ---- registration / presets
    def detect_presets(self) -> list[str]:
        if self.a.presets and self.a.presets != "auto":
            return [p.strip() for p in self.a.presets.split(",") if p.strip()]
        if self.a.dry_run:
            return list(PRESETS)
        return self.models.presets()

    def register(self) -> None:
        body = {"worker_id": self.worker_id, "hostname": socket.gethostname(), "gpu": self.gpu or "unknown",
                "vram_gb": self.vram, "presets": self.presets, "version": VERSION}
        if self.seeder and self.seeder.url:
            body.update(seed_url=self.seeder.url, seed_version=self.seeder.seed_version)
        _, r = self.api.call("POST", "/api/workers/register", body)
        if isinstance(r, dict) and r.get("poll_interval_s"):
            self.poll_s = float(r["poll_interval_s"])
        self.registered.set()
        log.info("registered as %s (gpu=%s, %s GB, presets=%s, poll=%ss)", self.worker_id, self.gpu,
                 self.vram, self.presets, self.poll_s)

    def register_until_ok(self, attempts: int = 0) -> str | None:
        """Retry forever (attempts=0) or up to `attempts`; returns None on success, else the last error."""
        delay, i, err = 2.0, 0, "stopped"
        while not self.stop.is_set():
            i += 1
            try:
                self.register()
                return None
            except ApiError as e:
                err = str(e) + (" (check --token)" if e.status in (401, 403) else "")
                log.error("register failed: %s", err)
            except OSError as e:
                err = f"server {self.api.base} unreachable ({e})"
                log.warning("%s; retry in %.0fs", err, delay)
            if attempts and i >= attempts:
                return err
            self.stop.wait(delay)
            delay = min(delay * 2, 60)
        return err

    # ---- heartbeat thread
    def seed_loop(self) -> None:
        while not self.stop.is_set():
            try:
                if self.seeder.refresh():
                    self.register()
            except Exception as e:  # seeding must never break rendering
                log.warning("seeding check failed: %s", e)
            self.stop.wait(self.a.seed_check_s)

    def heartbeat_loop(self) -> None:
        while not self.stop.is_set():
            self.wake.wait(self.a.heartbeat_s)
            self.wake.clear()
            if self.stop.is_set():
                break
            self.heartbeat()

    def heartbeat(self, timeout: float = 30) -> None:
        with self.lock:
            tid, status = self.task_id, self.status
            prog = self.progress
            if self.log_path and status == "busy":
                live = progress_of(self.log_path)
                if live:
                    prog = live
            if status == "busy" and self.task_t0:
                m, s = divmod(int(time.time() - self.task_t0), 60)
                prog = f"{m}m{s:02d}s | {prog or 'starting'}"
        try:
            _, r = self.api.call("POST", f"/api/workers/{self.worker_id}/heartbeat",
                                 {"status": status, "task_id": tid, "progress": prog}, timeout=timeout)
        except ApiError as e:
            if e.status == 404:  # server restarted / forgot us
                log.warning("heartbeat: server does not know %s; re-registering", self.worker_id)
                try:
                    self.register()
                except Exception as e2:
                    log.warning("re-register failed: %s", e2)
            else:
                log.warning("heartbeat failed: %s", e)
            return
        except OSError as e:
            log.warning("heartbeat: server unreachable (%s)", e)
            return
        if isinstance(r, dict) and r.get("cancel") and tid:
            self.request_cancel(tid)

    def request_cancel(self, tid: str) -> None:
        with self.lock:
            if self.task_id != tid or self.cancel.is_set():
                return
            log.warning("task %s cancelled by server: killing render", tid)
            self.cancel.set()
            if self.proc:
                kill_tree(self.proc)

    def set_state(self, status: str, task_id: str | None = None, progress: str | None = None) -> None:
        with self.lock:
            self.status, self.task_id, self.progress = status, task_id, progress
            if status == "idle":
                self.proc, self.log_path, self.task_t0 = None, None, 0.0
        self.wake.set()  # send a heartbeat right away on state changes

    # ---- task execution
    def claim(self) -> dict | None:
        try:
            status, r = self.api.call("POST", "/api/tasks/claim", {"worker_id": self.worker_id})
        except ApiError as e:
            if e.status == 404 and "task" not in e.body.lower():
                self.register()
            raise
        return r if status == 200 and isinstance(r, dict) and r.get("task_id") else None

    def run_task(self, task: dict) -> str:
        tid = str(task["task_id"])
        params = task.get("params") or {}
        self.cancel.clear()
        self.set_state("busy", tid, "downloading inputs")
        with self.lock:
            self.task_t0 = time.time()
        log.info("claimed task %s (project %s shot %s, %s %ss %s seed %s)", tid, task.get("project_id"),
                 task.get("shot_id"), params.get("preset"), params.get("seconds"), params.get("aspect"),
                 params.get("seed"))
        wd = self.work / (re.sub(r"[^\w.-]", "_", tid) or "task")
        shutil.rmtree(wd, ignore_errors=True)
        (wd / "inputs").mkdir(parents=True, exist_ok=True)

        try:
            image_args = self.fetch_inputs(task.get("inputs") or {}, wd / "inputs")
        except Exception as e:
            self.fail(tid, f"input download failed: {e}", "Check that the server still has the input file "
                                                         "and that the token is correct.", [])
            return "failed"
        if self.cancel.is_set():
            return "cancelled"

        cmd = self.build_cmd(params, image_args, wd)
        log.info("running: %s", " ".join(f'"{c}"' if " " in c else c for c in cmd[:12]) + " ...")
        env = dict(os.environ, LTXGEN_ROOT=str(self.root), PYTHONIOENCODING="utf-8")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0  # Ctrl+C stays with us
        with self.lock:
            self.log_path = wd / "log.txt"
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                         encoding="utf-8", errors="replace", env=env, creationflags=flags,
                                         start_new_session=os.name != "nt")
            proc = self.proc
            if self.cancel.is_set():
                kill_tree(proc)
        self.set_state("busy", tid, "rendering")
        try:
            out, err = proc.communicate()
        except KeyboardInterrupt:
            kill_tree(proc)
            self.fail(tid, "worker stopped by operator (Ctrl+C) during render", None,
                      tail_lines(wd / "log.txt"), attempts=1)
            raise
        if self.cancel.is_set():
            log.info("task %s cancelled; render killed, nothing uploaded", tid)
            return "cancelled"

        res = parse_result(out)
        if res is None:
            res = {"ok": False, "error": f"ltxgen produced no JSON (exit {proc.returncode})",
                   "error_tail": (err or out or "").strip().splitlines()[-25:]}
        (wd / "result.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
        video = Path(res.get("video") or wd / "video.mp4")
        if res.get("ok") and video.is_file():
            log.info("task %s rendered in %ss -> uploading %.1f MB", tid, res.get("elapsed_s"),
                     video.stat().st_size / 1e6)
            self.set_state("busy", tid, "uploading")
            return "completed" if self.complete(tid, out.strip(), video, wd) else "failed"
        err_msg = res.get("error") or f"render failed (exit {proc.returncode})"
        if res.get("missing"):
            err_msg += ": " + ", ".join(Path(m).name for m in res["missing"])
        if res.get("ok"):
            err_msg = "ltxgen reported ok but video.mp4 is missing"
        tail = res.get("error_tail") or tail_lines(wd / "log.txt") or (err or "").strip().splitlines()[-25:]
        log_text = (wd / "log.txt").read_text("utf-8", errors="replace") if (wd / "log.txt").exists() else ""
        abrupt = "Traceback" not in log_text + (err or "")  # native crash mid-load: no Python error
        if ("invalid python storage" in log_text + (err or "") or abrupt) and not task.get("_retried"):
            # transient weight-loading failure: a plain retry renders fine
            log.warning("task %s: transient 'invalid python storage' -> retrying once", tid)
            task["_retried"] = True
            return self.run_task(task)
        log.error("task %s failed: %s", tid, err_msg)
        self.fail(tid, err_msg, res.get("hint"), tail)
        return "failed"

    def fetch_inputs(self, inputs: dict, dest: Path) -> list[list[str]]:
        images = []
        self._ingredients = None
        spec = inputs.get("ingredients")
        if spec:  # reference sheet for the Ingredients IC-LoRA (character / prop / location consistency)
            path = dest / ("reference_sheet" + (Path(urllib.parse.urlparse(spec["url"]).path).suffix or ".png"))
            n = self.api.download(spec["url"], path)
            log.info("downloaded reference sheet (%d bytes)", n)
            self._ingredients = (str(path), str(spec.get("desc") or ""), str(spec.get("strength", 1.0)))
        for name in ("start_image", "end_image"):
            spec = inputs.get(name)
            if not spec:
                continue
            ext = Path(urllib.parse.urlparse(spec["url"]).path).suffix or ".png"
            path = dest / f"{name}{ext}"
            n = self.api.download(spec["url"], path)
            log.info("downloaded %s (%d bytes)", name, n)
            img = [str(path), str(int(spec.get("frame") or 0)), str(spec.get("strength", 1.0))]
            if spec.get("binding"):
                img.append(str(spec["binding"]))
            images.append(img)
        return images

    def build_cmd(self, p: dict, images: list[list[str]], wd: Path) -> list[str]:
        preset = p.get("preset", "draft")
        if self.a.dry_run:
            cmd = [sys.executable, str(Path(__file__).resolve()), "_fake_generate"]
        else:
            cmd = [sys.executable, str(self.ltxgen_path), "generate"]
        cmd += ["--prompt", str(p.get("prompt", "")), "--preset", preset, "--aspect", str(p.get("aspect", "16:9")),
                "--out-dir", str(wd), "--name", wd.name]
        if p.get("seed") is not None:
            cmd += ["--seed", str(int(p["seed"]))]
        if p.get("seconds"):
            cmd += ["--seconds", str(p["seconds"])]
        elif not self.a.dry_run and not self.models.has("duration_head"):
            log.warning("no seconds given and duration head missing: using 5 s")
            cmd += ["--seconds", "5"]
        for img in images:
            cmd += ["--image", *img]
        if getattr(self, "_ingredients", None):
            sheet, desc, strength = self._ingredients
            cmd += ["--ingredients", sheet, "--sheet-desc", desc, "--ingredients-strength", strength]
        if not self.a.dry_run:
            cmd += self.models.extra_args(preset)
        return cmd

    # ---- results
    def post_retry(self, what: str, fn, attempts: int = 8) -> bool:
        delay = 3.0
        for i in range(1, attempts + 1):
            try:
                fn()
                return True
            except ApiError as e:
                log.error("%s rejected: %s", what, e)
                if e.status not in (408, 429) and e.status < 500:
                    return False
            except OSError as e:
                log.warning("%s: server unreachable (%s) [attempt %d/%d]", what, e, i, attempts)
            if i < attempts:
                time.sleep(delay)
                delay = min(delay * 2, 60)
        return False

    def complete(self, tid: str, result_json: str, video: Path, wd: Path) -> bool:
        files = {"video": video}
        for k, name in (("sheet", "sheet.jpg"), ("log", "log.txt")):
            if (wd / name).is_file():
                files[k] = wd / name

        def send() -> None:
            ctype, length, body = multipart({"worker_id": self.worker_id, "result": result_json}, files)
            self.api.call("POST", f"/api/tasks/{tid}/complete", data=body, length=length,
                          headers={"Content-Type": ctype}, timeout=600)
        ok = self.post_retry(f"complete {tid}", send)
        if ok:
            log.info("task %s uploaded", tid)
        else:
            self.fail(tid, "upload of the finished video failed", f"video is kept on worker {self.worker_id} "
                      f"at {video}", tail_lines(wd / "log.txt"), attempts=3)
        return ok

    def fail(self, tid: str, error: str, hint: str | None, log_tail: list[str], attempts: int = 8) -> None:
        body = {"worker_id": self.worker_id, "error": error, "hint": hint, "log_tail": log_tail[-25:]}
        self.post_retry(f"fail {tid}", lambda: self.api.call("POST", f"/api/tasks/{tid}/fail", body), attempts)

    # ---- housekeeping / main loop
    def cleanup(self, days: float = 2) -> None:
        cutoff = time.time() - days * 86400
        for d in self.work.iterdir() if self.work.exists() else []:
            try:
                if d.is_dir() and d.name != self.task_id and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    log.info("removed old work dir %s", d)
            except OSError:
                pass

    def run(self) -> dict:
        keep_awake(not self.a.allow_sleep)
        self.cleanup()
        err = self.register_until_ok(4 if self.a.once else 0)
        if err:
            keep_awake(False)
            return {"ok": False, "worker_id": self.worker_id, "presets": self.presets, "outcome": "error",
                    "task_id": None, "error": err}
        hb = threading.Thread(target=self.heartbeat_loop, daemon=True, name="heartbeat")
        hb.start()
        if self.seeder:
            threading.Thread(target=self.seed_loop, daemon=True, name="seed-check").start()
        self.wake.set()
        summary: dict = {"ok": True, "worker_id": self.worker_id, "presets": self.presets, "outcome": "no_task",
                         "task_id": None}
        delay, last_detect = 2.0, time.time()
        try:
            while not self.stop.is_set():
                try:
                    if time.time() - last_detect > 300:  # models may have finished copying meanwhile
                        last_detect = time.time()
                        new = self.detect_presets()
                        if new != self.presets:
                            self.presets = new
                            self.register()
                    task = self.claim()
                    delay = 2.0
                    if task is None:
                        if self.a.once:
                            break
                        self.stop.wait(self.poll_s)
                        continue
                    try:
                        outcome = self.run_task(task)
                    finally:
                        self.set_state("idle")
                    log.info("task %s -> %s", task["task_id"], outcome)
                    summary.update(ok=outcome == "completed", outcome=outcome, task_id=task["task_id"])
                    self.cleanup()
                    if self.a.once:
                        break
                except ApiError as e:
                    log.error("server error: %s%s; retry in %.0fs", e,
                              " (bad token?)" if e.status in (401, 403) else "", delay)
                    summary.update(ok=False, outcome="error", error=str(e))
                    if self.a.once:
                        break
                    self.stop.wait(delay)
                    delay = min(delay * 2, 60)
                except OSError as e:
                    log.warning("server unreachable (%s); retry in %.0fs", e, delay)
                    self.stop.wait(delay)
                    delay = min(delay * 2, 60)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    log.exception("unexpected error; continuing in %.0fs", delay)
                    self.stop.wait(delay)
                    delay = min(delay * 2, 60)
        except KeyboardInterrupt:
            log.info("stopping (Ctrl+C)")
            summary.update(outcome="interrupted")
        finally:
            self.stop.set()
            self.set_state("idle")
            hb.join(10)  # let an in-flight heartbeat finish so ours is the last one
            self.heartbeat(timeout=5)  # final idle heartbeat so the dashboard does not show us busy
            keep_awake(False)
        return summary


def parse_result(out: str) -> dict | None:
    out = (out or "").strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        i = out.find("{")
        try:
            return json.loads(out[i:]) if i >= 0 else None
        except json.JSONDecodeError:
            return None


# --------------------------------------------------------------------------- dry-run generator
def fake_generate(argv: list[str]) -> None:
    """Stand-in for `ltxgen.py generate` (same args, files and JSON). Env: LTX_FAKE_SECONDS, LTX_FAKE_FAIL."""
    p = argparse.ArgumentParser()
    for k in ("--prompt", "--preset", "--aspect", "--out-dir", "--name"):
        p.add_argument(k)
    p.add_argument("--seconds", type=float, default=2.0)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--image", nargs="+", action="append")
    a, _ = p.parse_known_args(argv)
    d = Path(a.out_dir)
    d.mkdir(parents=True, exist_ok=True)
    video, logf = d / "video.mp4", d / "log.txt"
    render_s, steps, t0 = float(os.environ.get("LTX_FAKE_SECONDS", "4")), 8, time.time()
    ff = shutil.which("ffmpeg")
    (d / "meta.json").write_text(json.dumps({"prompt": a.prompt, "preset": a.preset, "seed": a.seed,
                                             "images": a.image or [], "dry_run": True}, indent=2), encoding="utf-8")
    with open(logf, "w", encoding="utf-8") as lg:
        lg.write(f"[dry-run] preset={a.preset} aspect={a.aspect} seconds={a.seconds} seed={a.seed}\n")
        for img in a.image or []:
            if not Path(img[0]).is_file():
                print(json.dumps({"ok": False, "error": f"image not found: {img[0]}"}))
                sys.exit(2)
            lg.write(f"[dry-run] conditioning image {img}\n")
        for i in range(1, steps + 1):
            time.sleep(render_s / steps)
            lg.write(f"\rDenoising: {i * 100 // steps:3d}%| step {i}/{steps}")
            lg.flush()
        lg.write("\n")
        if os.environ.get("LTX_FAKE_FAIL") or not ff:
            lg.write("torch.OutOfMemoryError: CUDA out of memory (simulated)\n" if ff else "ffmpeg not found\n")
            lg.close()
            print(json.dumps({"ok": False, "video": None, "dir": str(d), "seed": a.seed,
                              "elapsed_s": round(time.time() - t0, 1), "log": str(logf),
                              "error_tail": tail_lines(logf),
                              "hint": "GPU OOM: use preset draft, fewer seconds, --offload disk, or "
                                      "--vae video_vae_conv."}, indent=2))
            sys.exit(1)
        src = (["-loop", "1", "-i", a.image[0][0]] if a.image else ["-f", "lavfi", "-i", "testsrc2=size=256x144:rate=24"])
        secs = str(a.seconds or 2)
        cmd = [ff, "-y", "-v", "error", *src, "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
               "-t", secs, "-vf", "scale=256:144,format=yuv420p", "-r", "24", "-c:v", "libx264", "-preset", "ultrafast",
               "-c:a", "aac", "-shortest", str(video)]
        r = subprocess.run(cmd, stdout=lg, stderr=subprocess.STDOUT)
        if r.returncode == 0:
            subprocess.run([ff, "-y", "-v", "error", "-i", str(video), "-vf", "fps=2,scale=128:-2,tile=4x1",
                            "-frames:v", "1", str(d / "sheet.jpg")], stdout=lg, stderr=subprocess.STDOUT)
    ok = r.returncode == 0 and video.exists()
    res = {"ok": ok, "video": str(video) if ok else None, "dir": str(d), "seed": a.seed,
           "elapsed_s": round(time.time() - t0, 1), "log": str(logf), "dry_run": True}
    if ok and (d / "sheet.jpg").exists():
        res["sheet"] = str(d / "sheet.jpg")
    if not ok:
        res["error_tail"] = tail_lines(logf)
    print(json.dumps(res, indent=2))
    sys.exit(0 if ok else 1)


# --------------------------------------------------------------------------- main
def setup_logging(work: Path, verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        work.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(work / "worker.log", maxBytes=5_000_000,
                                                             backupCount=3, encoding="utf-8"))
    except OSError as e:
        print(f"warning: cannot write {work / 'worker.log'}: {e}", file=sys.stderr)
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "_fake_generate":
        return fake_generate(sys.argv[2:])
    env = os.environ.get
    default_ltxgen = HERE.parent / "ltxgen.py"
    p = argparse.ArgumentParser(prog="worker", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default=env("LTX_STUDIO_SERVER"), help="studio URL, e.g. http://MAINPC:7860 "
                   "(env LTX_STUDIO_SERVER)")
    p.add_argument("--token", default=env("LTX_STUDIO_TOKEN"), help="shared token (env LTX_STUDIO_TOKEN)")
    p.add_argument("--worker-id", default=env("LTX_STUDIO_WORKER_ID"), help="default <hostname>-<gpu>, e.g. pc2-5080")
    p.add_argument("--root", default=env("LTXGEN_ROOT", str(Path(__file__).resolve().parents[2])), help="install root (env LTXGEN_ROOT)")
    p.add_argument("--ltxgen", help="path to ltxgen.py (default: ../ltxgen.py, else <root>/ltxgen/ltxgen.py)")
    p.add_argument("--presets", default="auto", help="'auto' (from model files) or comma list, e.g. draft,standard")
    p.add_argument("--once", action="store_true", help="claim at most one task, then exit (prints JSON)")
    p.add_argument("--dry-run", action="store_true", help="fake renders with a tiny ffmpeg clip (no GPU/models)")
    p.add_argument("--heartbeat-s", type=float, default=20.0)
    p.add_argument("--seed", dest="seed", action="store_true", default=None,
                   help="serve model/code files to joining PCs (default: only when this PC has the full manifest)")
    p.add_argument("--no-seed", dest="seed", action="store_false")
    p.add_argument("--seed-port", type=int, default=7861)
    p.add_argument("--seed-check-s", type=float, default=120, help="manifest poll interval for seeding")
    p.add_argument("--allow-sleep", action="store_true", help="do not keep Windows awake while running")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    if not a.server or not a.token:
        p.error("--server and --token are required (or set LTX_STUDIO_SERVER / LTX_STUDIO_TOKEN)")
    if not a.ltxgen:
        a.ltxgen = str(default_ltxgen if default_ltxgen.exists() else Path(a.root) / "ltxgen" / "ltxgen.py")
    setup_logging(Path(a.root) / "studio_worker", a.verbose)
    w = Worker(a)
    log.info("LTX Studio worker v%s | server %s | root %s | ltxgen %s%s", VERSION, w.api.base, w.root,
             a.ltxgen, " | DRY RUN" if a.dry_run else "")
    if not a.dry_run and not w.presets:
        log.warning("no runnable presets (model files / venv missing) - the server will not hand out tasks")
    summary = w.run()
    if a.once:
        print(json.dumps(summary, indent=2))
        sys.exit(0 if summary["ok"] else 1)


if __name__ == "__main__":
    main()
