"""LTX Studio one-click join: copy everything from the main PC + already-joined peers, install, verify, join.

Stdlib only; started by join-ltx-studio.bat via `uv run --no-project --python 3.13 join.py ...` before any
venv exists. Steps (each re-runnable, downloads resume from <root>/.join_state.json):
  a. manifest + peers from the studio server        (PROTOCOL.md "Peer distribution", v1.1)
  b. disk-space check
  c. parallel piece download (64 MiB pieces, sha256-verified, spread over all peers)
  d. Python env: uv sync of LTX-2 + Windows extras (triton-windows, ltx_kernels .pth, all2all stub, nvfp4 pyd)
  e. verify (torch + CUDA + NVFP4 kernel, ltxgen doctor)
  f. join: firewall rule for the seeder port, Scheduled Task "LTX Studio Worker", wait until online

  python join.py --server http://MAINPC:7860 --token T --root E:\\ltx-studio [--dry-run|--skip-env|--skip-join]
"""
from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures as cf
import ctypes
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TASK_NAME = "LTX Studio Worker"
FW_RULE = "LTX Studio seeder (TCP {port})"
STATE_NAME = ".join_state.json"
GB = 1e9
LOG_FILE: Path | None = None
_print_lock = threading.Lock()


def say(msg: str = "", end: str = "\n") -> None:
    with _print_lock:
        sys.stdout.write(msg + end)
        sys.stdout.flush()
        if LOG_FILE and end == "\n":
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg.strip("\r") + "\n")
            except OSError:
                pass


def step(title: str) -> None:
    say("")
    say(f"==> {title}")


class Abort(Exception):
    """Fatal, user-facing error (printed without traceback)."""


# --------------------------------------------------------------------------- HTTP
class Http:
    def __init__(self, server: str, token: str):
        self.base = server.rstrip("/")
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def open(self, url: str, headers: dict | None = None, timeout: float = 30):
        url = url if url.startswith("http") else self.base + url
        req = urllib.request.Request(url, headers={"X-LTX-Token": self.token, **(headers or {})})
        return self.opener.open(req, timeout=timeout)

    def json(self, path: str, timeout: float = 60):
        with self.open(path, timeout=timeout) as r:
            return json.loads(r.read() or b"null")

    def range(self, url: str, start: int, length: int, timeout: float = 60) -> bytes:
        hdr = {"Range": f"bytes={start}-{start + length - 1}"} if length else {}
        with self.open(url, hdr, timeout) as r:
            if length and r.status != 206 and not (r.status == 200 and start == 0):
                raise OSError(f"peer ignored Range (HTTP {r.status})")
            buf = bytearray()
            while len(buf) < length:
                chunk = r.read(min(1 << 20, length - len(buf)))
                if not chunk:
                    break
                buf += chunk
        if len(buf) != length:
            raise OSError(f"short read {len(buf)}/{length}")
        return bytes(buf)


def get_manifest(http: Http) -> dict:
    while True:
        try:
            m = http.json("/api/dist/manifest", timeout=120)
            if m and m.get("files") is not None:
                return m
            raise OSError("empty manifest")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise Abort("the server rejected the token; download a fresh join file from the dashboard") from None
            if e.code == 503:
                say("    server is still building the manifest (hashing model files) - retrying in 10 s")
            elif e.code == 404:
                raise Abort("server has no /api/dist/manifest (studio server too old?)") from None
            else:
                say(f"    manifest: HTTP {e.code} - retrying in 10 s")
        except (OSError, ValueError) as e:
            say(f"    cannot reach {http.base} ({e}) - retrying in 10 s")
        time.sleep(10)


# --------------------------------------------------------------------------- downloader
def set_sparse(path: Path) -> None:
    """Mark a file sparse on NTFS so preallocating 40 GB does not write 40 GB of zeros (best effort)."""
    if os.name != "nt":
        return
    try:
        import msvcrt
        with open(path, "r+b") as f:
            h = msvcrt.get_osfhandle(f.fileno())
            n = ctypes.c_ulong()
            ctypes.windll.kernel32.DeviceIoControl(ctypes.c_void_p(h), 0x000900C4, None, 0, None, 0,
                                                   ctypes.byref(n), None)  # FSCTL_SET_SPARSE
    except Exception:
        pass


class Peer:
    def __init__(self, pid: str, url: str):
        self.id, self.url = pid, url
        self.rate: float | None = None   # bytes/s, EWMA
        self.active = self.fails = self.done = 0
        self.banned_until = 0.0
        self.listed = True
        self.missing: set[str] = set()
        self.kinds: set[str] | None = None   # None = everything; else e.g. {"model", "tool"} (stale code)

    def usable(self, now: float) -> bool:
        return self.listed and now >= self.banned_until


class Job:
    __slots__ = ("f", "idx", "offset", "length", "hash", "tried", "attempts")

    def __init__(self, f: dict, idx: int, piece: int):
        self.f, self.idx, self.hash = f, idx, f["pieces"][idx]
        self.offset = idx * piece
        self.length = min(piece, f["size"] - self.offset)
        self.tried: set[str] = set()
        self.attempts = 0


class Downloader:
    def __init__(self, http: Http, root: Path, manifest: dict, threads: int = 10, ban_after: int = 3,
                 refresh_s: float = 60, ban_s: float = 600, max_attempts: int = 25):
        self.http, self.root, self.m = http, root, manifest
        self.piece = int(manifest["piece_size"])
        self.threads, self.ban_after, self.refresh_s, self.ban_s = threads, ban_after, refresh_s, ban_s
        self.max_attempts = max_attempts
        self.lock = threading.RLock()
        self.save_lock = threading.Lock()
        self.state_path = root / STATE_NAME
        self.state = self.load_state()
        self.dirty = False
        self.peers: dict[str, Peer] = {}
        self.jobs: queue.Queue[Job | None] = queue.Queue()
        self.remaining: dict[str, int] = {}     # path -> pieces still missing
        self.total = sum(f["size"] for f in manifest["files"])
        self.done_bytes = 0
        self.fetched = 0
        self.window: collections.deque = collections.deque()
        self.errors: list[str] = []
        self.fatal: str | None = None
        self.stop = threading.Event()
        self.pending = 0
        self.stale = False  # a piece from main no longer matched: manifest must be re-synced

    # ---- state (keyed by path + size + piece index + piece hash, so a manifest version bump
    #      only invalidates pieces that really changed)
    def load_state(self) -> dict:
        try:
            s = json.loads(self.state_path.read_text("utf-8"))
            if isinstance(s.get("files"), dict):
                return s
        except (OSError, ValueError):
            pass
        return {"files": {}}

    def save_state(self, force: bool = False) -> None:
        with self.save_lock:  # snapshot + write under one lock so an older snapshot never wins
            with self.lock:
                if not (self.dirty or force):
                    return
                self.state["version"] = self.m.get("version")
                data = json.dumps(self.state)
                self.dirty = False
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(data, "utf-8")
            os.replace(tmp, self.state_path)

    def verified(self, f: dict) -> dict[str, str]:
        e = self.state["files"].get(f["path"])
        if not e or e.get("size") != f["size"]:
            e = self.state["files"][f["path"]] = {"size": f["size"], "pieces": {}}
            self.dirty = True
        good = {k: v for k, v in e["pieces"].items() if k.isdigit() and int(k) < len(f["pieces"])
                and f["pieces"][int(k)] == v}
        if len(good) != len(e["pieces"]):
            e["pieces"] = good
            self.dirty = True
        return good

    def mark(self, f: dict, idx: int) -> None:
        self.state["files"][f["path"]]["pieces"][str(idx)] = f["pieces"][idx]
        self.dirty = True

    # ---- planning
    def plan(self, hash_threads: int = 4, dry: bool = False) -> dict:
        """Classify every file; hash-check unknown local data. Returns {path: [missing piece idx]}."""
        todo: dict[str, list[int]] = {}
        to_hash: list[tuple[dict, Path, list[int]]] = []
        for f in self.m["files"]:
            final = self.root / f["path"]
            part = final.with_name(final.name + ".part")
            n = len(f["pieces"])
            known = self.state["files"].get(f["path"])
            ok = self.verified(f)
            if final.is_file() and final.stat().st_size == f["size"] and len(ok) == n:
                continue                                         # complete + recorded
            if final.is_file() and not part.exists():
                local, src = final, "existing"
            elif part.exists():
                local, src = part, "part"
            else:
                todo[f["path"]] = list(range(n))
                continue
            size = local.stat().st_size
            if src == "part" and known and known.get("size") == f["size"]:
                todo[f["path"]] = [i for i in range(n) if str(i) not in ok]  # trust our own records
                continue
            # unknown data (pre-copied file, lost state): hash the pieces that are fully present once
            check = [i for i in range(n) if i * self.piece + min(self.piece, f["size"] - i * self.piece) <= size]
            to_hash.append((f, local, check))
            todo[f["path"]] = [i for i in range(n) if i not in check]
        if to_hash:
            gb = sum(min(len(c) * self.piece, f["size"]) for f, _, c in to_hash) / GB
            say(f"    hash-checking {len(to_hash)} existing file(s), {gb:.1f} GB ...")
            with cf.ThreadPoolExecutor(hash_threads) as ex:
                futs = {ex.submit(self.hash_local, f, p, c): (f, p) for f, p, c in to_hash}
                for fut in cf.as_completed(futs):
                    f, p = futs[fut]
                    good = fut.result()
                    bad = [i for i in range(len(f["pieces"])) if i not in good and i not in todo[f["path"]]]
                    todo[f["path"]] = sorted(todo[f["path"]] + bad)
                    with self.lock:
                        for i in good:
                            self.mark(f, i)
                    say(f"    {f['path']}: {len(good)}/{len(f['pieces'])} pieces already valid")
        if not dry:
            self.prepare_files(todo)
        return {k: v for k, v in todo.items() if v}

    def hash_local(self, f: dict, path: Path, idxs: list[int]) -> list[int]:
        good = []
        with open(path, "rb") as fh:
            for i in idxs:
                fh.seek(i * self.piece)
                if hashlib.sha256(fh.read(min(self.piece, f["size"] - i * self.piece))).hexdigest() == f["pieces"][i]:
                    good.append(i)
        return good

    def prepare_files(self, todo: dict[str, list[int]]) -> None:
        for f in self.m["files"]:
            final = self.root / f["path"]
            part = final.with_name(final.name + ".part")
            missing = todo.get(f["path"])
            if missing is None:
                continue
            final.parent.mkdir(parents=True, exist_ok=True)
            if not missing:
                if part.exists() and final.exists():
                    final.unlink()
                if part.exists():
                    os.replace(part, final)
                continue
            if final.exists() and not part.exists():
                os.replace(final, part)                          # partly valid local copy: keep good pieces
            elif final.exists():
                final.unlink()
            if not part.exists():
                part.touch()
                set_sparse(part)
            if part.stat().st_size != f["size"]:
                with open(part, "r+b") as fh:
                    fh.truncate(f["size"])

    # ---- peers
    def refresh_peers(self) -> None:
        try:
            lst = self.http.json("/api/dist/peers", timeout=20)
        except Exception as e:
            say(f"\r    (peer list refresh failed: {e})")
            return
        with self.lock:
            ids = set()
            for p in lst or []:
                pid, url = str(p.get("id")), str(p.get("url") or "")
                if not url:
                    continue
                ids.add(pid)
                if pid not in self.peers:
                    self.peers[pid] = Peer(pid, url)
                    if self.fetched:
                        say(f"\r    new peer: {pid} ({url})" + " " * 20)
                self.peers[pid].url, self.peers[pid].listed = url, True
                self.peers[pid].kinds = set(p["kinds"]) if p.get("kinds") else None
            for pid, p in self.peers.items():
                if pid not in ids:
                    p.listed = False

    def pick(self, job: Job) -> Peer | None:
        now = time.time()
        with self.lock:
            cands = [p for p in self.peers.values() if p.usable(now) and job.f["path"] not in p.missing
                     and (p.kinds is None or job.f["kind"] in p.kinds)]
            fresh = [p for p in cands if p.id not in job.tried]
            if not fresh and cands:
                job.tried.clear()
                fresh = cands
            if not fresh:
                return None
            # explore unmeasured peers first (round robin by load), then least expected finish time
            unmeasured = [p for p in fresh if p.rate is None and p.active < 2]
            if unmeasured:
                best = min(unmeasured, key=lambda p: (p.active, p.done))
            else:
                rates = [p.rate for p in fresh if p.rate] or [1.0]
                med = sorted(rates)[len(rates) // 2]
                best = min(fresh, key=lambda p: (p.active + 1) / (p.rate or med))
            best.active += 1
            return best

    # ---- workers
    def worker(self) -> None:
        while not self.stop.is_set():
            try:
                job = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                return
            peer = self.pick(job)
            if peer is None:
                self.jobs.put(job)
                self.stop.wait(2)
                continue
            path = job.f["path"]
            t0 = time.time()
            err, soft = None, False
            try:
                data = self.http.range(peer.url + urllib.parse.quote(path), job.offset, job.length)
                if hashlib.sha256(data).hexdigest() != job.hash:
                    err = "sha256 mismatch"
            except urllib.error.HTTPError as e:
                err, soft = f"HTTP {e.code}", e.code in (404, 503)
                if e.code == 404:
                    with self.lock:
                        peer.missing.add(path)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            dt = max(time.time() - t0, 1e-3)
            authoritative = peer.id == "main"
            if err == "sha256 mismatch" and authoritative:
                # The main PC's copy changed after our manifest snapshot (code edited there): not corruption.
                # Drop the piece; the post-download manifest re-sync fetches the new version of the file.
                with self.lock:
                    peer.active -= 1
                    self.stale = True
                    self.pending -= 1
                continue
            with self.lock:
                peer.active -= 1
                job.tried.add(peer.id)
                if err and authoritative and not soft:
                    # main is the source of truth and often the only peer: never ban it, just back off
                    # (connection refused while its studio server restarts, timeouts under load, ...)
                    peer.fails += 1
                    peer.banned_until = time.time() + min(2 * peer.fails, 20)
                    if peer.fails in (3, 10, 30):
                        say(f"\r    main PC not responding ({err}); retrying..." + " " * 20)
                    err, soft = err, True
                if err:
                    if not soft:
                        peer.fails += 1
                        job.attempts += 1
                        self.errors.append(f"{peer.id} {path}#{job.idx}: {err}")
                        if peer.fails >= self.ban_after and peer.banned_until <= time.time():
                            peer.banned_until = time.time() + self.ban_s
                            say(f"\r    peer {peer.id} banned for {self.ban_s:.0f} s after {peer.fails} "
                                f"consecutive failures (last: {err})" + " " * 10)
                    if job.attempts >= self.max_attempts:
                        self.fatal = f"{path} piece {job.idx} failed {job.attempts} times (last: {err})"
                        self.stop.set()
            if err:
                self.stop.wait(0.5 if soft else 0.1)
                self.jobs.put(job)
                continue
            with self.lock:
                peer.fails = 0
                peer.done += 1
                r = job.length / dt
                peer.rate = r if peer.rate is None else 0.7 * peer.rate + 0.3 * r
            try:
                part = self.root / (path + ".part")
                with open(part, "r+b") as fh:
                    fh.seek(job.offset)
                    fh.write(data)
            except OSError as e:
                self.fatal = f"cannot write {path}: {e}"
                self.stop.set()
                return
            with self.lock:
                self.mark(job.f, job.idx)
                self.done_bytes += job.length
                self.fetched += job.length
                self.window.append((time.time(), job.length))
                self.remaining[path] -= 1
                last = self.remaining[path] == 0
                self.pending -= 1
            if last:
                self.finish(job.f)

    def finish(self, f: dict) -> None:
        final = self.root / f["path"]
        part = final.with_name(final.name + ".part")
        try:
            if final.exists():
                try:
                    final.unlink()
                except PermissionError:  # running exe (e.g. tools/uv.exe): Windows allows renaming it
                    old = final.with_name(final.name + f".old{int(time.time())}")
                    os.replace(final, old)
            os.replace(part, final)
        except OSError as e:
            with self.lock:
                self.errors.append(f"cannot finalize {f['path']}: {e}")
                self.fatal = self.fatal or f"cannot rename {part.name} -> {final.name}: {e} (file in use?)"
        self.save_state(force=True)

    # ---- progress / main loop
    def status_line(self) -> str:
        now = time.time()
        with self.lock:
            while self.window and now - self.window[0][0] > 10:
                self.window.popleft()
            span = max(now - self.window[0][0], 1.0) if self.window else 1.0
            speed = sum(b for _, b in self.window) / span if self.window else 0.0
            act = sum(1 for p in self.peers.values() if p.active > 0)
            usable = sum(1 for p in self.peers.values() if p.usable(now))
            pct = 100.0 * self.done_bytes / self.total if self.total else 100.0
            left = self.total - self.done_bytes
        eta = time.strftime("%H:%M:%S", time.gmtime(left / speed)) if speed > 0 else "--:--:--"
        return (f"\r    {pct:5.1f}%  {self.done_bytes / GB:7.2f}/{self.total / GB:.2f} GB  "
                f"{speed / 1e6:7.1f} MB/s  ETA {eta}  peers {act} active/{usable} usable   ")

    def run(self, todo: dict[str, list[int]]) -> bool:
        files = {f["path"]: f for f in self.m["files"]}
        missing_bytes = 0
        for path, idxs in todo.items():
            f = files[path]
            self.remaining[path] = len(idxs)
            for i in idxs:
                j = Job(f, i, self.piece)
                missing_bytes += j.length
                self.jobs.put(j)
        self.pending = sum(len(v) for v in todo.values())
        self.done_bytes = self.total - missing_bytes
        if not self.pending:
            return True
        self.refresh_peers()
        say(f"    {self.pending} pieces / {missing_bytes / GB:.2f} GB from {len(self.peers)} peer(s): "
            + ", ".join(self.peers))
        threads = [threading.Thread(target=self.worker, daemon=True) for _ in range(self.threads)]
        for t in threads:
            t.start()
        last_refresh = last_save = time.time()
        last_progress, stall_since = self.done_bytes, time.time()
        try:
            while self.pending > 0 and not self.stop.is_set():
                time.sleep(1)
                say(self.status_line(), end="")
                now = time.time()
                if now - last_refresh >= self.refresh_s:
                    last_refresh = now
                    self.refresh_peers()
                if now - last_save >= 3:
                    last_save = now
                    self.save_state()
                if self.done_bytes != last_progress:
                    last_progress, stall_since = self.done_bytes, now
                elif now - stall_since > 900:
                    self.fatal = "no progress for 15 minutes (no reachable peer has the missing pieces)"
                    break
        finally:
            self.stop.set()
            for _ in threads:
                self.jobs.put(None)
            for t in threads:
                t.join(timeout=70)
            self.save_state(force=True)
            say(self.status_line())
        if self.fatal:
            raise Abort(self.fatal)
        return self.pending == 0


# --------------------------------------------------------------------------- env / verify / join helpers
def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None, check: bool = True,
        capture: bool = False) -> subprocess.CompletedProcess:
    say("    $ " + " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd))
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
                       capture_output=capture)
    if check and r.returncode != 0:
        tail = ((r.stdout or "") + (r.stderr or "")).strip()[-1500:] if capture else ""
        raise Abort(f"command failed (exit {r.returncode}): {cmd[0]} {cmd[1] if len(cmd) > 1 else ''}\n{tail}")
    return r


def base_env(root: Path) -> dict:
    env = dict(os.environ)
    env["UV_PYTHON_INSTALL_DIR"] = str(root / "tools" / "python")
    env["UV_CACHE_DIR"] = str(root / "tools" / "uv-cache")
    env["LTXGEN_ROOT"] = str(root)
    env["PATH"] = str(root / "tools") + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def setup_env(root: Path, m: dict) -> Path:
    uv = root / "tools" / "uv.exe"
    repo = root / "LTX-2"
    if not uv.exists():
        uv = Path(shutil.which("uv") or uv)
    pyds = [f["path"] for f in m["files"] if re.match(r"tools/nvfp4_cpp\.cp\d+-win_amd64\.pyd$", f["path"])]
    envinfo = m.get("env") or {}
    py = envinfo.get("python")
    tag = re.search(r"\.cp(\d)(\d+)-", pyds[0]).groups() if pyds else None
    if not py and tag:
        py = f"{tag[0]}.{tag[1]}"
    py = py or "3.13"
    if tag and f"{tag[0]}.{tag[1]}" != py:
        raise Abort(f"prebuilt kernel {pyds[0]} is for Python {tag[0]}.{tag[1]} but the main PC venv is {py}")
    if not (repo / "uv.lock").exists():
        raise Abort(f"{repo / 'uv.lock'} missing (it should come from the main PC with the code files)")
    env = base_env(root)
    say(f"    Python {py} for the LTX-2 venv (uv-managed, in {root / 'tools' / 'python'}); "
        f"wheel cache {root / 'tools' / 'uv-cache'}")
    run([uv, "sync", "--frozen", "--inexact", "--extra", "natten", "--python", py], cwd=repo, env=env)
    vpy = repo / ".venv" / "Scripts" / "python.exe"
    got = run([vpy, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"], env=env, capture=True).stdout.strip()
    if got != py:
        raise Abort(f"LTX-2 venv is Python {got}, expected {py}; delete {repo / '.venv'} and re-run")
    triton = envinfo.get("triton_windows")
    run([uv, "pip", "install", "--python", vpy, f"triton-windows=={triton}" if triton else "triton-windows", "ninja"],
        cwd=repo, env=env)
    sp = repo / ".venv" / "Lib" / "site-packages"
    (sp / "ltx_kernels_src.pth").write_text(str(repo / "packages" / "ltx-kernels" / "src") + "\n", encoding="ascii")
    shutil.copyfile(root / "ltxgen" / "all2all_cpp_stub.py", sp / "all2all_cpp.py")
    for p in pyds:
        shutil.copyfile(root / p, sp / Path(p).name)
        say(f"    installed {Path(p).name}")
    if not pyds:
        say("    [warn] no prebuilt nvfp4 kernel in the manifest; NVFP4 will be unavailable")
    return vpy


def verify(root: Path, vpy: Path) -> list[str]:
    env = base_env(root)
    r = run([vpy, "-c", "import torch; from ltx_kernels import nvfp4; "
             "assert torch.cuda.is_available(), 'CUDA not available'; "
             "assert nvfp4.is_available(), 'NVFP4 kernel not available'; "
             "print(torch.__version__, torch.cuda.get_device_name(0))"], env=env, check=False, capture=True)
    if r.returncode != 0:
        smi = subprocess.run(["nvidia-smi"], capture_output=True, text=True).stdout
        m = re.search(r"Driver Version:\s*([\d.]+)\s+CUDA Version:\s*([\d.]+)", smi)
        drv = f"driver {m.group(1)} (supports CUDA {m.group(2)})" if m else "the installed driver"
        raise Abort("GPU check failed:\n" + (r.stdout + r.stderr).strip()[-1200:] +
                    f"\nHint: {drv}. LTX Studio needs NVIDIA driver 580 or newer (CUDA 13): update it from the "
                    "NVIDIA App or https://www.nvidia.com/Download/index.aspx, reboot, and re-run.")
    say(f"    [ok] torch {r.stdout.strip()} + NVFP4 kernel")
    r = run([vpy, root / "ltxgen" / "ltxgen.py", "doctor"], env=env, check=False, capture=True)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        raise Abort("ltxgen doctor failed:\n" + (r.stdout + r.stderr)[-1200:]) from None
    problems = [k for k in ("repo", "venv_python", "ffmpeg", "ltx_kernels") if not d.get(k)]
    if not (d.get("torch") or {}).get("cuda"):
        problems.append("torch.cuda")
    if problems:
        raise Abort(f"ltxgen doctor reports problems: {problems}")
    sys.path.insert(0, str(root / "ltxgen" / "studio"))
    import worker  # noqa: E402  (arrived with the code files)
    presets = worker.Models(root, worker.load_ltxgen(root / "ltxgen" / "ltxgen.py")).presets()
    if not presets:
        missing = [k for k, v in d.get("files", {}).items() if not v.get("exists")]
        raise Abort(f"model files incomplete, no preset can run (missing: {missing})")
    say(f"    [ok] doctor clean; this PC can render: {', '.join(presets)}")
    return presets


def powershell(script: str, check: bool = True) -> subprocess.CompletedProcess:
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-EncodedCommand", enc], capture_output=True, text=True, errors="replace")
    if check and r.returncode != 0:
        raise Abort("PowerShell step failed:\n" + (r.stdout + r.stderr).strip()[-1500:])
    return r


def ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def join(a: argparse.Namespace, root: Path, vpy: Path) -> None:
    sys.path.insert(0, str(root / "ltxgen" / "studio"))
    import worker  # noqa: E402
    gpu, _ = worker.gpu_info()
    wid = worker.default_worker_id(gpu)
    rule = FW_RULE.format(port=a.seed_port)

    say(f"    firewall: inbound TCP {a.seed_port} (Private profile only) for the seeder")
    r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule}"], capture_output=True)
    if r.returncode != 0:
        run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={rule}", "dir=in", "action=allow",
             "protocol=TCP", f"localport={a.seed_port}", "profile=private"], capture=True)
    pub = powershell("Get-NetConnectionProfile | Where-Object NetworkCategory -eq 'Public' | "
                     "ForEach-Object { $_.InterfaceAlias }", check=False).stdout.split()
    if pub:
        say(f"    [warn] network(s) {pub} are 'Public': other PCs cannot fetch from this seeder. If this is your "
            f"home LAN: Set-NetConnectionProfile -InterfaceAlias '{pub[0]}' -NetworkCategory Private")

    wdir = root / "studio_worker"
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "worker_settings.json").write_text(json.dumps({"server": a.server, "token": a.token, "root": str(root)},
                                                          indent=2), encoding="utf-8")
    cmd = wdir / "start_worker.cmd"
    cmd.write_text("\n".join([
        "@echo off",
        "title LTX Studio Worker",
        f'set "LTXGEN_ROOT={root}"',
        f'set "PATH={root}\\tools;%PATH%"',
        ":loop",
        f'"{vpy}" "{root}\\ltxgen\\studio\\worker.py" --server {a.server} --token {a.token} --root "{root}" '
        f"--seed --seed-port {a.seed_port}",
        "echo worker exited (code %errorlevel%), restarting in 15 s ...",
        "timeout /t 15 /nobreak >nul",
        "goto loop", ""]), encoding="ascii")
    say(f"    wrote {cmd}")

    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
    say(f"    Scheduled Task '{TASK_NAME}': at logon of {user}, highest privileges, restart on failure")
    powershell(f"""
$ErrorActionPreference = 'Stop'
$name = {ps_quote(TASK_NAME)}
if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {{ Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue }}
Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -and ($_.CommandLine -like {ps_quote('*' + str(cmd) + '*')} -or
  ($_.CommandLine -like '*worker.py*' -and $_.CommandLine -like {ps_quote('*' + str(root) + '*')})) }} |
  ForEach-Object {{ & taskkill /T /F /PID $_.ProcessId 2>$null | Out-Null }}
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument {ps_quote('/c "' + str(cmd) + '"')} -WorkingDirectory {ps_quote(str(wdir))}
$trigger = New-ScheduledTaskTrigger -AtLogOn -User {ps_quote(user)}
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId {ps_quote(user)} -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
  -Description 'LTX Studio render worker + seeder (installed by join-ltx-studio.bat)' -Force | Out-Null
Start-ScheduledTask -TaskName $name
""")
    say(f"    worker started; waiting for '{wid}' to come online at {a.server} ...")
    http = Http(a.server, a.token)
    t0 = time.time()
    while time.time() - t0 < a.online_timeout:
        try:
            for w in http.json("/api/workers", timeout=10) or []:
                if w.get("id") == wid and w.get("status") != "offline":
                    say(f"    [ok] {wid} is online ({w.get('gpu')}, presets {w.get('presets')})")
                    return
        except Exception:
            pass
        time.sleep(5)
    raise Abort(f"worker '{wid}' did not show up online within {a.online_timeout:.0f} s; "
                f"see {wdir / 'worker.log'}")


# --------------------------------------------------------------------------- main
def main() -> int:
    global LOG_FILE
    p = argparse.ArgumentParser(prog="join", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--threads", type=int, default=10)
    p.add_argument("--ban-after", type=int, default=3, help="consecutive failures before a peer is banned")
    p.add_argument("--ban-s", type=float, default=600)
    p.add_argument("--refresh-s", type=float, default=60, help="peer list refresh interval")
    p.add_argument("--seed-port", type=int, default=7861)
    p.add_argument("--online-timeout", type=float, default=240)
    p.add_argument("--dry-run", action="store_true", help="only show what would be downloaded/done")
    p.add_argument("--skip-env", action="store_true", help="download only: no uv sync / verify")
    p.add_argument("--skip-join", action="store_true", help="no firewall rule / scheduled task")
    a = p.parse_args()
    a.server = a.server.rstrip("/")
    root = Path(a.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    LOG_FILE = None if a.dry_run else root / "join.log"
    say(f"LTX Studio join | server {a.server} | root {root}" + (" | DRY RUN" if a.dry_run else ""))
    http = Http(a.server, a.token)
    t_start = time.time()
    try:
        step("1/6 Manifest and peers")
        m = get_manifest(http)
        kinds = collections.Counter()
        for f in m["files"]:
            kinds[f["kind"]] += f["size"]
        say(f"    manifest {m.get('version')}: {len(m['files'])} files, {sum(kinds.values()) / GB:.1f} GB "
            f"({', '.join(f'{k} {v / GB:.1f} GB' for k, v in sorted(kinds.items()))}), "
            f"piece {int(m['piece_size']) >> 20} MiB")
        peers = http.json("/api/dist/peers")
        say(f"    {len(peers)} peer(s): " + ", ".join(f"{x['id']} ({x['url']})" for x in peers))

        for attempt in range(6):
            dl = Downloader(http, root, m, a.threads, a.ban_after, a.refresh_s, a.ban_s)
            step("2/6 Disk space")
            todo = dl.plan(dry=a.dry_run)
            need = sum(min(dl.piece, f["size"] - i * dl.piece) for f in m["files"] for i in todo.get(f["path"], []))
            margin = 0 if a.skip_env else 25 * GB      # venv + wheel cache
            free = shutil.disk_usage(root).free
            say(f"    to download: {need / GB:.2f} GB in {sum(map(len, todo.values()))} pieces "
                f"({len(todo)} files); free on {root.anchor}: {free / GB:.1f} GB")
            if free < need + margin:
                raise Abort(f"not enough disk space: need {(need + margin) / GB:.1f} GB "
                            f"(incl. {margin / GB:.0f} GB for the Python env), have {free / GB:.1f} GB")
            if not a.dry_run:
                dl.save_state(force=True)
            if a.dry_run:
                for path, idx in list(todo.items())[:40]:
                    say(f"      {path}  ({len(idx)} pieces)")
                if len(todo) > 40:
                    say(f"      ... and {len(todo) - 40} more files")
                say("")
                say("DRY RUN: nothing downloaded, installed or registered.")
                return 0
            step("3/6 Download (parallel, sha256-verified pieces)")
            t0 = time.time()
            dl.run(todo)
            dt = time.time() - t0
            say(f"    [ok] downloaded {dl.fetched / GB:.2f} GB in {dt:.0f} s "
                f"({dl.fetched / max(dt, 1e-3) / 1e6:.0f} MB/s); "
                + "; ".join(f"{p.id}: {p.done} pieces" + (" (banned)" if p.banned_until > time.time() else "")
                            for p in dl.peers.values()))
            if dl.errors:
                say(f"    {len(dl.errors)} piece error(s) were retried on other peers, e.g. {dl.errors[0]}")
            m2 = get_manifest(http)
            if m2.get("version") == m.get("version") and not dl.stale:
                break
            if dl.stale and m2.get("version") == m.get("version"):
                say("    some files changed on the main PC during download; waiting for its new file list ...")
                for _ in range(30):
                    time.sleep(10)
                    m2 = get_manifest(http)
                    if m2.get("version") != m.get("version"):
                        break
            say(f"    manifest changed during download ({m.get('version')} -> {m2.get('version')}); syncing again")
            m = m2
        else:
            raise Abort("manifest keeps changing on the server; try again later")

        if a.skip_env:
            say("")
            say(f"SUCCESS (download only, --skip-env) in {time.time() - t_start:.0f} s")
            return 0
        step("4/6 Python environment (needs internet for wheels)")
        vpy = setup_env(root, m)
        step("5/6 Verify")
        verify(root, vpy)
        if a.skip_join:
            say("")
            say(f"SUCCESS (installed + verified, --skip-join) in {time.time() - t_start:.0f} s")
            return 0
        step("6/6 Join the farm")
        join(a, root, vpy)
        say("")
        say(f"SUCCESS: this PC is an LTX Studio worker. Dashboard: {a.server}/  (took {time.time() - t_start:.0f} s)")
        return 0
    except Abort as e:
        say("")
        say(f"FAILED: {e}")
        say("Fix the problem and run the join file again - it resumes where it stopped.")
        return 1
    except KeyboardInterrupt:
        say("")
        say("FAILED: interrupted. Run the join file again to resume.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
