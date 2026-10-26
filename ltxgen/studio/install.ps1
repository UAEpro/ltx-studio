<#
.SYNOPSIS
  One-step LTX Studio install. Run on a fresh Windows 11 PC with an NVIDIA GPU:

    powershell -ExecutionPolicy Bypass -File install.ps1 [-Root D:\video]

  It only does the minimum needed to open the web app (uv + the studio's small Python environment) and then
  starts the studio and opens http://localhost:7860/#/setup. Everything else - Git, FFmpeg, CUDA, the LTX engine,
  the ~50 GB of models, the render worker - installs from the Setup page in the browser, with live progress.
  Re-running is safe.
#>
param([string]$Root = "", [int]$Port = 7860, [switch]$NoBrowser)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ltxgen = Split-Path -Parent $here
if (-not $Root) { $Root = Split-Path -Parent $ltxgen }
function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }

Step "LTX Studio install in $Root"
if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
  Write-Host "    [warn] nvidia-smi not found - install the NVIDIA driver first (rendering needs an NVIDIA GPU)." -ForegroundColor Yellow
}

Step "uv (Python manager)"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install -e --id astral-sh.uv --accept-package-agreements --accept-source-agreements --disable-interactivity
  } else {
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  }
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
uv --version

Step "Studio Python environment ($ltxgen\.venv)"
Push-Location $ltxgen
uv sync --inexact
Pop-Location
$py = Join-Path $ltxgen ".venv\Scripts\python.exe"
& $py -c "import fastapi, uvicorn; print('studio deps ok')"

Step "Desktop shortcut"
$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "LTX Studio.lnk"
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = "powershell.exe"
$s.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$here\start_studio.ps1`""
$s.WorkingDirectory = $Root
$s.IconLocation = "shell32.dll,115"
$s.Save()
Write-Host "    $lnk"

Step "Starting LTX Studio on http://localhost:$Port"
Start-Process -WindowStyle Minimized -FilePath $py -ArgumentList "`"$here\server.py`"", "--port", "$Port"
Start-Sleep -Seconds 4
if (-not $NoBrowser) { Start-Process "http://localhost:$Port/#/setup" }
Write-Host ""
Write-Host "Done. Finish the setup in the browser (Setup page -> Install everything)." -ForegroundColor Green
