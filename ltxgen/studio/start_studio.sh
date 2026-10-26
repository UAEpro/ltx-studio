#!/usr/bin/env bash
# Starts LTX Studio on this computer: http://localhost:7860  (Linux / macOS; works from any install folder)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$(dirname "$HERE")/.venv/bin/python" "$HERE/server.py" --host 0.0.0.0 --port "${PORT:-7860}"
