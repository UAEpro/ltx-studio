<#
.SYNOPSIS
  Starts the LTX Studio worker on this PC and restarts it if it ever exits.
  Defaults come from <Root>\studio_worker\worker_settings.json (written by setup_worker_pc.ps1),
  then env LTX_STUDIO_SERVER / LTX_STUDIO_TOKEN; parameters override both.

  powershell -ExecutionPolicy Bypass -File start_worker.ps1 [-Server http://MAINPC:7860] [-Token T] [-DryRun]
#>
param(
  [string]$Root = "",
  [string]$Server = "",
  [string]$Token = "",
  [string]$WorkerId = "",
  [string]$Presets = "auto",
  [switch]$DryRun,
  [switch]$NoRestart
)
if (-not $Root) { $Root = if ($env:LTXGEN_ROOT) { $env:LTXGEN_ROOT } else { "D:\video" } }
$settingsFile = Join-Path $Root "studio_worker\worker_settings.json"
if (Test-Path $settingsFile) {
  $s = Get-Content $settingsFile -Raw | ConvertFrom-Json
  if (-not $Server) { $Server = $s.server }
  if (-not $Token) { $Token = $s.token }
}
if (-not $Server) { $Server = $env:LTX_STUDIO_SERVER }
if (-not $Token) { $Token = $env:LTX_STUDIO_TOKEN }
if (-not $Server -or -not $Token) { Write-Host "Need -Server and -Token (or run setup_worker_pc.ps1 with them)." -ForegroundColor Red; exit 1 }

# worker.py lives in <Root>\ltxgen\studio; prefer the copy next to this script.
$worker = Join-Path (Split-Path $PSScriptRoot -Parent) "worker.py"
if (-not (Test-Path $worker)) { $worker = Join-Path $Root "ltxgen\studio\worker.py" }

# Any real Python >= 3.10 works (worker is stdlib only); fall back to the LTX-2 venv interpreter.
$py = $null
foreach ($c in @(@("py", "-3.12"), @("python"), @("python3"), @((Join-Path $Root "LTX-2\.venv\Scripts\python.exe")))) {
  if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
  $extra = @($c | Select-Object -Skip 1)
  & $c[0] @extra -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
  if ($LASTEXITCODE -eq 0) { $py = $c; break }
}
if (-not $py) { Write-Host "No Python >= 3.10 found." -ForegroundColor Red; exit 1 }
$pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)

$env:LTXGEN_ROOT = $Root
$wargs = @($worker, "--server", $Server, "--token", $Token, "--root", $Root, "--presets", $Presets)
if ($WorkerId) { $wargs += @("--worker-id", $WorkerId) }
if ($DryRun) { $wargs += "--dry-run" }
$Host.UI.RawUI.WindowTitle = "LTX Studio Worker -> $Server"
Write-Host "LTX Studio worker: $pyExe $worker -> $Server (log: $Root\studio_worker\worker.log). Ctrl+C to stop."
while ($true) {
  & $pyExe @pyArgs @wargs
  $rc = $LASTEXITCODE
  if ($NoRestart -or $rc -eq 0 -or $rc -eq 2) { break }   # 2 = bad arguments
  Write-Host "worker exited with code $rc; restarting in 15 s (Ctrl+C to stop)" -ForegroundColor Yellow
  Start-Sleep -Seconds 15
}
