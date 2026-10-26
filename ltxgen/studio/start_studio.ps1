# Starts LTX Studio on this PC: http://localhost:7860  (works from any install folder)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path (Split-Path -Parent $here) ".venv\Scripts\python.exe"
& $py (Join-Path $here "server.py") --host 0.0.0.0 --port 7860
