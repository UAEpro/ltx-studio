@echo off
rem ======================================================================
rem  LTX Studio - join this PC to the render farm (one click).
rem  Double-click it. It asks for administrator rights, copies everything
rem  from the main PC and the other PCs, installs, verifies, and starts the
rem  worker. Safe to run again at any time: it resumes where it stopped.
rem  Optional: "join-ltx-studio.bat /plan" only prints what it would do.
rem ======================================================================
setlocal EnableExtensions DisableDelayedExpansion
set "LTX_SERVER=__SERVER__"
set "LTX_TOKEN=__TOKEN__"
set "LTX_BAT=%~f0"
set "LTX_PLAN="
set "LTX_NOPAUSE="
for %%A in (%*) do (
  if /i "%%~A"=="/plan" set "LTX_PLAN=1"
  if /i "%%~A"=="/nopause" set "LTX_NOPAUSE=1"
)
title LTX Studio - join render farm
if defined LTX_PLAN goto run

net session >nul 2>&1
if not errorlevel 1 goto run
echo Requesting administrator rights ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Start-Process -FilePath $env:LTX_BAT -Verb RunAs -ErrorAction Stop } catch { exit 1 }"
if errorlevel 1 (
  echo.
  echo FAILED: administrator rights are required ^(firewall rule + scheduled task^).
  echo Right-click the file and choose "Run as administrator".
  pause
)
exit /b

:run
rem Everything after the PS1 marker line below is PowerShell 5.1; cmd never reads past "exit /b".
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = [IO.File]::ReadAllText($env:LTX_BAT); $i = $s.LastIndexOf('#PS1' + '_BEGIN'); if ($i -lt 0) { exit 3 }; Invoke-Expression $s.Substring($i)"
set "RC=%errorlevel%"
if not defined LTX_NOPAUSE pause
exit /b %RC%

#PS1_BEGIN
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[System.Net.WebRequest]::DefaultWebProxy = New-Object System.Net.WebProxy   # LAN only: no system proxy
$server = $env:LTX_SERVER.TrimEnd('/')
$token = $env:LTX_TOKEN
$plan = [bool]$env:LTX_PLAN
$need = 160GB

function Banner($text, $color) {
  Write-Host ''
  Write-Host ('=' * 70) -ForegroundColor $color
  Write-Host "  $text" -ForegroundColor $color
  Write-Host ('=' * 70) -ForegroundColor $color
}
function Fail($msg) {
  Write-Host ''
  Write-Host $msg -ForegroundColor Red
  Banner 'FAILED - fix the problem above and run this file again (it resumes).' Red
  exit 1
}

Write-Host 'LTX Studio - join this PC to the render farm'
Write-Host "  server : $server"
if ($server.StartsWith('__') -or $token.StartsWith('__')) {
  Fail 'This is the template file. Download the join file from the studio dashboard (Workers tab).'
}

# ---- 1. GPU / driver
$smi = (Get-Command nvidia-smi -ErrorAction SilentlyContinue)
if (-not $smi) { Fail 'No NVIDIA driver found (nvidia-smi is missing). Install the latest NVIDIA driver from nvidia.com/drivers, then run this file again.' }
$gpu = (& nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>$null | Select-Object -First 1)
if (-not $gpu) { Fail 'nvidia-smi found but it reports no GPU. Reinstall the NVIDIA driver and reboot.' }
Write-Host "  GPU    : $gpu"
# The farm's PyTorch is built for CUDA 13.x: the driver must support CUDA 13 (R580 or newer).
$smiText = (& nvidia-smi 2>$null | Out-String)
$cudaMax = if ($smiText -match 'CUDA Version:\s*([0-9]+)\.([0-9]+)') { [int]$Matches[1] } else { 0 }
$drvVer = (& nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>$null | Select-Object -First 1).Trim()
function Get-DriverCuda {
  $t = (& nvidia-smi 2>$null | Out-String)
  if ($t -match 'CUDA Version:\s*([0-9]+)\.([0-9]+)') { return [int]$Matches[1] } else { return 0 }
}
function Update-NvidiaDriver($gpuName) {
  # Ask NVIDIA's official driver service for the newest WHQL driver for this exact GPU, download it from
  # download.nvidia.com, verify NVIDIA's Authenticode signature, then install silently.
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $web = New-Object System.Net.WebClient
  $web.Proxy = [System.Net.WebRequest]::GetSystemWebProxy()
  $web.Headers.Add('User-Agent', 'Mozilla/5.0 (LTX Studio join)')
  $model = ($gpuName -replace '^NVIDIA\s+', '' -replace '^GeForce\s+', '').Trim()
  [xml]$products = $web.DownloadString('https://www.nvidia.com/Download/API/lookupValueSearch.aspx?TypeID=3')
  $norm = { param($n) ($n -replace '^\s*NVIDIA\s+', '' -replace '^GeForce\s+', '').Trim() }
  $hits = @($products.LookupValueSearch.LookupValues.LookupValue | Where-Object { (& $norm $_.Name) -eq $model })
  if (-not $hits) { throw "GPU '$model' not found in NVIDIA's product list" }
  [xml]$series = $web.DownloadString('https://www.nvidia.com/Download/API/lookupValueSearch.aspx?TypeID=2&ParentID=1')
  $desktop = @($series.LookupValueSearch.LookupValues.LookupValue | Where-Object { $_.Name -notmatch 'Notebook' } | ForEach-Object { $_.Value })
  $hit = ($hits | Where-Object { $desktop -contains $_.ParentID } | Select-Object -First 1)
  if (-not $hit) { $hit = $hits[0] }
  $q = "func=DriverManualLookup&psid=$($hit.ParentID)&pfid=$($hit.Value)&osID=135&languageCode=1033&isWHQL=1&dch=1&upCRD=0&qnf=0&sort1=0&numberOfResults=1"
  $info = ($web.DownloadString("https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/services/AjaxDriverService.php?$q") | ConvertFrom-Json).IDS[0].downloadInfo
  $url = [string]$info.DownloadURL
  if ($gpuName -match 'GeForce' -and ([uri]::UnescapeDataString([string]$info.Name) -notmatch 'GeForce' -or $url -match 'quadro')) {
    throw "NVIDIA returned a non-GeForce driver ($($info.Name)) for $gpuName"
  }
  if ($url -notmatch '^https://[a-z0-9.-]*download\.nvidia\.com/') { throw "unexpected driver URL: $url" }
  Write-Host "  Latest NVIDIA driver for $model : $($info.Version) ($($info.DownloadURLFileSize))"
  for ($i = 10; $i -gt 0; $i--) {
    Write-Host -NoNewline "`r  Installing it automatically in $i s (press Ctrl+C to cancel) "
    Start-Sleep 1
  }
  Write-Host ''
  $dir = Join-Path $env:TEMP 'ltx-driver'
  New-Item -ItemType Directory -Force $dir | Out-Null
  $exe = Join-Path $dir ([IO.Path]::GetFileName($url))
  if (-not (Test-Path $exe)) {
    Write-Host '  Downloading driver (about 1 GB) ...'
    $web.DownloadFile($url, "$exe.part")
    Move-Item -Force "$exe.part" $exe
  }
  $sig = Get-AuthenticodeSignature $exe
  if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'NVIDIA Corporation') {
    Remove-Item -Force $exe
    throw "driver installer signature check failed ($($sig.Status)); deleted it"
  }
  Write-Host '  Signature OK (NVIDIA Corporation). Installing silently - the screen may flicker ...'
  $p = Start-Process -FilePath $exe -ArgumentList '-s', '-noreboot' -Wait -PassThru
  Write-Host "  Driver installer finished (exit code $($p.ExitCode))."
}

if ($cudaMax -lt 13) {
  Write-Host ''
  Write-Host "  NVIDIA driver $drvVer only supports CUDA $cudaMax.x; LTX Studio needs driver 580+ (CUDA 13). Updating it ..." -ForegroundColor Yellow
  if ($plan) {
    Write-Host '  (plan mode: would download and silently install the latest NVIDIA driver, then continue)'
  } else {
    try {
      Update-NvidiaDriver ((& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1).Trim())
    } catch {
      Write-Host "  Automatic driver update failed: $_" -ForegroundColor Yellow
      Write-Host '  Install it manually: NVIDIA App > Drivers, or https://www.nvidia.com/Download/index.aspx, then reboot and run this file again.' -ForegroundColor Yellow
      Start-Process 'https://www.nvidia.com/Download/index.aspx'
      Fail "NVIDIA driver $drvVer is too old (need driver 580+ / CUDA 13)."
    }
    $cudaMax = Get-DriverCuda
    $drvVer = (& nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>$null | Select-Object -First 1).Trim()
    if ($cudaMax -lt 13) {
      # The new driver becomes active after a reboot: resume this join file automatically at next logon.
      $cmd = 'cmd.exe /c "' + $env:LTX_BAT + '"'
      New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'LTXStudioJoin' -Value $cmd -PropertyType String -Force | Out-Null
      Banner 'Driver installed. Rebooting in 60 s - the join continues automatically after you log in.' Yellow
      Write-Host '  (Cancel the reboot with:  shutdown /a   - then reboot later yourself; the join still resumes at logon.)'
      shutdown.exe /r /t 60 /c "LTX Studio: rebooting to activate the new NVIDIA driver. The join resumes after login."
      exit 0
    }
  }
}
Write-Host "  Driver : $drvVer (CUDA $cudaMax.x OK)"

# ---- 2. install root: existing <drive>:\ltx-studio (resume) or the fixed drive with most free space
$disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Sort-Object FreeSpace -Descending)
$existing = $disks | Where-Object { Test-Path (Join-Path ($_.DeviceID + '\') 'ltx-studio') } | Select-Object -First 1
if ($existing) {
  $root = Join-Path ($existing.DeviceID + '\') 'ltx-studio'
  Write-Host ("  root   : {0} (exists - resuming; {1:N0} GB free)" -f $root, ($existing.FreeSpace / 1GB))
} else {
  if (-not $disks.Count) { Fail 'No fixed disk found.' }
  $d = $disks[0]
  $root = Join-Path ($d.DeviceID + '\') 'ltx-studio'
  Write-Host ("  root   : {0} ({1:N0} GB free, drive with most space)" -f $root, ($d.FreeSpace / 1GB))
  if ($d.FreeSpace -lt $need) {
    Fail ("Not enough disk space: {0} has {1:N0} GB free, about {2:N0} GB are needed (models + Python env). Free some space and run again." -f $d.DeviceID, ($d.FreeSpace / 1GB), ($need / 1GB))
  }
}
$uv = Join-Path $root 'tools\uv.exe'
$joinPy = Join-Path $root 'ltxgen\studio\join.py'

function Get-DistFile($rel, $dest) {
  $url = "$server/api/dist/file/$rel"
  $tmp = "$dest.download"
  New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
  for ($i = 1; $i -le 5; $i++) {
    try {
      Invoke-WebRequest -UseBasicParsing -Uri $url -Headers @{ 'X-LTX-Token' = $token } -OutFile $tmp -TimeoutSec 900
      Move-Item -Force $tmp $dest
      return
    } catch {
      $code = $null
      if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
      if ($code -eq 401 -or $code -eq 403) { Fail 'The server rejected the token. Download a fresh join file from the dashboard.' }
      Write-Host "  download of $rel failed ($($_.Exception.Message)); retry $i/5 in 5 s ..." -ForegroundColor Yellow
      Start-Sleep -Seconds 5
    }
  }
  Fail "Cannot download $rel from $server. Is the studio server running, and is TCP 7860 allowed in the main PC firewall (Private network)?"
}

if ($plan) {
  Write-Host ''
  Write-Host 'PLAN (nothing is changed):'
  try {
    $peers = Invoke-RestMethod -UseBasicParsing -Uri "$server/api/dist/peers" -Headers @{ 'X-LTX-Token' = $token } -TimeoutSec 15
    Write-Host ("  server reachable, token accepted, {0} peer(s): {1}" -f @($peers).Count, ((@($peers) | ForEach-Object { $_.id }) -join ', '))
  } catch { Write-Host "  [warn] server check failed: $($_.Exception.Message)" -ForegroundColor Yellow }
  Write-Host "  1. download $server/api/dist/file/tools/uv.exe            -> $uv"
  Write-Host "  2. download $server/api/dist/file/ltxgen/studio/join.py   -> $joinPy"
  Write-Host "  3. $uv run --no-project --python 3.13 $joinPy --server $server --token *** --root $root"
  Write-Host "     (join.py: manifest -> parallel download from all peers -> uv sync -> verify -> firewall rule"
  Write-Host "      'LTX Studio seeder (TCP 7861)' Private -> Scheduled Task 'LTX Studio Worker' -> wait online)"
  Write-Host "  env: UV_PYTHON_INSTALL_DIR=$root\tools\python  UV_CACHE_DIR=$root\tools\uv-cache"
  exit 0
}

# ---- 3. bootstrap: uv + join.py from the server, then hand over to join.py
New-Item -ItemType Directory -Force -Path $root | Out-Null
Write-Host ''
Write-Host '==> Bootstrap: downloading uv and join.py from the main PC'
Get-DistFile 'tools/uv.exe' $uv
Get-DistFile 'ltxgen/studio/join.py' $joinPy
$env:UV_PYTHON_INSTALL_DIR = Join-Path $root 'tools\python'
$env:UV_CACHE_DIR = Join-Path $root 'tools\uv-cache'
$env:UV_NO_CONFIG = '1'
$ErrorActionPreference = 'Continue'
& $uv run --no-project --python 3.13 $joinPy --server $server --token $token --root $root
$rc = $LASTEXITCODE
if ($rc -eq 0) {
  Banner "SUCCESS - this PC is now an LTX Studio worker. Dashboard: $server/" Green
  Write-Host "  The worker starts automatically at logon (Scheduled Task 'LTX Studio Worker')."
  Write-Host "  Log: $root\studio_worker\worker.log    Keep this PC awake (no sleep) while rendering."
} else {
  Write-Host "  Details: $root\join.log" -ForegroundColor Red
  Banner "FAILED (exit $rc) - fix the problem above and run this file again (it resumes)." Red
}
exit $rc
