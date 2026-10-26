#!/usr/bin/env bash
# LTX Studio install for Linux and macOS. Run from the cloned repository:
#
#   bash ltxgen/studio/install.sh                      # studio + web app, finish setup in the browser
#   bash ltxgen/studio/install.sh --worker SERVER TOKEN # render-only PC: install everything, then join SERVER
#
# It only installs uv and the studio's small Python environment, then starts the studio and opens
# http://localhost:7860/#/setup, where the engine and models install with live progress. Packages that need an admin
# password (git, ffmpeg, a C++ compiler on Linux) are listed on the Setup page with the exact command to run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LTXGEN="$(dirname "$HERE")"
ROOT="$(dirname "$LTXGEN")"
PORT="${PORT:-7860}"
WORKER_SERVER=""; WORKER_TOKEN=""
if [[ "${1:-}" == "--worker" ]]; then WORKER_SERVER="${2:?server url}"; WORKER_TOKEN="${3:?token}"; fi
step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }

step "LTX Studio install in $ROOT ($(uname -s) $(uname -m))"
if command -v nvidia-smi >/dev/null; then nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
elif [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]]; then echo "Apple Silicon: renders with the full-precision model (64 GB+ memory recommended)"
else echo "[warn] no NVIDIA GPU or Apple Silicon found - this computer can run the studio and use other PCs to render"; fi
for t in git ffmpeg; do command -v "$t" >/dev/null || echo "[warn] $t not found - install it (Setup page shows the command)"; done

step "uv (Python manager)"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
uv --version

step "Studio Python environment ($LTXGEN/.venv)"
uv --directory "$LTXGEN" sync --inexact --python 3.13
PY="$LTXGEN/.venv/bin/python"
"$PY" -c "import fastapi, uvicorn; print('studio deps ok')"

if [[ -n "$WORKER_SERVER" ]]; then
  step "Render worker: engine + models (this takes a while - ~50-120 GB download)"
  "$PY" "$HERE/setup_page.py" --install-all
  step "Starting the render worker -> $WORKER_SERVER"
  mkdir -p "$ROOT/studio"
  nohup "$PY" "$HERE/worker.py" --server "$WORKER_SERVER" --token "$WORKER_TOKEN" >>"$ROOT/studio/worker.log" 2>&1 &
  echo "worker running (log: $ROOT/studio/worker.log). Start it again after a reboot with:"
  echo "  $PY $HERE/worker.py --server $WORKER_SERVER --token $WORKER_TOKEN"
  exit 0
fi

step "Starting LTX Studio on http://localhost:$PORT"
chmod +x "$HERE/start_studio.sh"
nohup "$HERE/start_studio.sh" >>"$ROOT/studio.log" 2>&1 &
sleep 4
URL="http://localhost:$PORT/#/setup"
if command -v open >/dev/null && [[ "$(uname -s)" == "Darwin" ]]; then open "$URL"
elif command -v xdg-open >/dev/null; then xdg-open "$URL" >/dev/null 2>&1 || true; fi
printf '\n\033[32mDone.\033[0m Open %s and press "Install everything".\n' "$URL"
echo "Start the studio again later with: $HERE/start_studio.sh"
