<#
.SYNOPSIS
  Prepares a secondary PC (RTX 50xx, Windows 11) as an LTX Studio render worker. Idempotent: re-run any time.

.DESCRIPTION
  1. GPU / NVIDIA driver check (nvidia-smi)
  2. winget installs if missing: Git, Python 3.12, uv, FFmpeg, CUDA toolkit 13.x, VS 2022 Build Tools (C++)
  3. LTX-2 repo clone at <Root>\LTX-2, pinned commit e98f6d9, `uv sync --extra natten`
  4. ltxgen folder copied from the main PC (\\<MainPC>\ltxgen), paths patched if Root is not D:\video
  5. Windows extras (triton-windows, ninja, ltx_kernels .pth, all2all stub, nvfp4_cpp kernel build)
  6. Model files: -ModelSource smb (default, robocopy from \\<MainPC>\ltx-models) or hf (HuggingFace)
  7. `ltxgen.py doctor` + worker self-tests (local fake render, then register/claim against the server)

  Run from an elevated PowerShell (CUDA / Build Tools installers need admin):
    powershell -ExecutionPolicy Bypass -File setup_worker_pc.ps1 -MainPC MAINPC -Token <token>

.PARAMETER Root        Install root on this PC (default D:\video). Same layout as the main PC.
.PARAMETER MainPC      Host name or IP of the main PC (SMB shares ltxgen + ltx-models, studio server).
.PARAMETER Server      Studio URL (default http://<MainPC>:7860).
.PARAMETER Token       Studio token (studio\studio.json on the main PC).
.PARAMETER ModelSource smb | hf
.PARAMETER WithQuality Also fetch the 42 GB bf16 transformer + detailing LoRA (enables the "quality" preset).
.PARAMETER SkipInstall Skip the winget step.
.PARAMETER SkipModels  Skip the model step.
#>
param(
  [string]$Root = "D:\video",
  [string]$MainPC = "",
  [string]$Server = "",
  [string]$Token = "",
  [ValidateSet("smb", "hf")][string]$ModelSource = "smb",
  [switch]$WithQuality,
  [switch]$SkipInstall,
  [switch]$SkipModels
)
$ErrorActionPreference = "Continue"
$LtxCommit = "e98f6d9"
$Repo = Join-Path $Root "LTX-2"
$LtxGen = Join-Path $Root "ltxgen"
$Models = Join-Path $Root "models\ltx-2.5"
$VenvPy = Join-Path $Repo ".venv\Scripts\python.exe"
$WorkDir = Join-Path $Root "studio_worker"
if (-not $Server -and $MainPC) { $Server = "http://${MainPC}:7860" }
$script:Problems = @()

function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Ok($t) { Write-Host "    [ok]   $t" -ForegroundColor Green }
function Info($t) { Write-Host "    $t" }
function Warn($t) { Write-Host "    [warn] $t" -ForegroundColor Yellow; $script:Problems += $t }
function Die($t) { Write-Host "    [FAIL] $t" -ForegroundColor Red; exit 1 }
function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Find-Cuda {
  $base = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA"
  Get-ChildItem $base -Directory -Filter "v13.*" -ErrorAction SilentlyContinue |
    Where-Object { Test-Path (Join-Path $_.FullName "bin\nvcc.exe") } |
    Sort-Object { [version]($_.Name.TrimStart("v")) } -Descending | Select-Object -First 1
}
function Find-VCTools {
  $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
  if (-not (Test-Path $vswhere)) { return $null }
  $p = & $vswhere -products * -version "[17.0,18.0)" -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
  if ($p) { return ($p | Select-Object -First 1) } else { return $null }
}
function Find-Python {
  # A real Python >= 3.10 (not the Microsoft Store stub). Returns @(exe, args...) or $null.
  $cands = @(@("py", "-3.12"), @("python"), @("python3"))
  foreach ($c in $cands) {
    if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
    $extra = @($c | Select-Object -Skip 1)
    $v = & $c[0] @extra -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $v -and ([version]"$v" -ge [version]"3.10")) { return ,$c }
  }
  if (Test-Path $VenvPy) { return ,@($VenvPy) }
  return $null
}
function Patch-File($path, $from, $to) {
  if (-not (Test-Path $path)) { return }
  $bytes = [IO.File]::ReadAllBytes($path)
  $bom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
  $text = [IO.File]::ReadAllText($path)
  $new = $text.Replace($from, $to)
  if ($new -ne $text) {
    [IO.File]::WriteAllText($path, $new, (New-Object Text.UTF8Encoding($bom)))
    Info "patched $(Split-Path $path -Leaf): '$from' -> '$to'"
  }
}

Write-Host "LTX Studio worker setup | Root=$Root MainPC=$MainPC Server=$Server ModelSource=$ModelSource WithQuality=$WithQuality"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Warn "not running elevated: CUDA / Build Tools installs and the nvfp4 build may fail. Re-run as Administrator if they do." }
New-Item -ItemType Directory -Force -Path $Root, $WorkDir | Out-Null

# ----------------------------------------------------------------------------- 1. GPU
Step "1/7 NVIDIA driver and GPU"
if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
  Die "nvidia-smi not found: install the latest NVIDIA Game Ready / Studio driver first (https://www.nvidia.com/drivers)."
}
$gpu = & nvidia-smi --query-gpu=name,driver_version,memory.total,compute_cap --format=csv,noheader 2>$null | Select-Object -First 1
if (-not $gpu) { Die "nvidia-smi found but reports no GPU." }
Ok "GPU: $gpu"
$cc = ($gpu -split ",")[-1].Trim()
if ($cc -and [double]$cc -lt 12.0) { Warn "compute capability $cc < 12.0: the NVFP4 kernel targets Blackwell (RTX 50xx, sm_120)." }

# ----------------------------------------------------------------------------- 2. tools
Step "2/7 Toolchain (winget)"
$tools = @(
  @{ Name = "Git"; Id = "Git.Git"; Test = { [bool](Get-Command git -ErrorAction SilentlyContinue) }; Args = @() },
  @{ Name = "Python 3.12"; Id = "Python.Python.3.12"; Test = { [bool](Find-Python) -and -not ((Find-Python)[0] -eq $VenvPy) }; Args = @() },
  @{ Name = "uv"; Id = "astral-sh.uv"; Test = { [bool](Get-Command uv -ErrorAction SilentlyContinue) }; Args = @() },
  @{ Name = "FFmpeg"; Id = "Gyan.FFmpeg"; Test = { [bool](Get-Command ffmpeg -ErrorAction SilentlyContinue) }; Args = @() },
  @{ Name = "CUDA toolkit 13.x"; Id = "Nvidia.CUDA"; Test = { [bool](Find-Cuda) }; Args = @() },
  @{ Name = "VS 2022 Build Tools (C++ workload)"; Id = "Microsoft.VisualStudio.2022.BuildTools"; Test = { [bool](Find-VCTools) };
     Args = @("--override", "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended") }
)
$missing = @($tools | Where-Object { -not (& $_.Test) })
foreach ($t in $tools) { if ($missing -notcontains $t) { Ok "$($t.Name) present" } }
if ($missing.Count -eq 0) {
  Ok "nothing to install"
} elseif ($SkipInstall) {
  Warn "missing (skipped by -SkipInstall): $(($missing | ForEach-Object { $_.Name }) -join ', ')"
} else {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { Die "winget not found (install 'App Installer' from the Microsoft Store)." }
  Info "Will install: $(($missing | ForEach-Object { "$($_.Name) [$($_.Id)]" }) -join ', ')"
  Info "(this accepts the winget package/source agreements; CUDA + Build Tools are multi-GB and take a while)"
  foreach ($t in $missing) {
    Info "winget install $($t.Id) ..."
    & winget install -e --id $t.Id --accept-package-agreements --accept-source-agreements --disable-interactivity @($t.Args)
    Refresh-Path
    if (& $t.Test) { Ok "$($t.Name) installed" } else { Warn "$($t.Name) still not detected (winget exit $LASTEXITCODE). A new shell or reboot may be needed." }
  }
}
Refresh-Path
$cuda = Find-Cuda
if ($cuda) { Ok "CUDA: $($cuda.FullName)" } else { Warn "CUDA 13.x toolkit not found (needed to build the nvfp4 kernel). If winget installed 12.x, install 13.x from developer.nvidia.com." }

# ----------------------------------------------------------------------------- 3. LTX-2 repo
Step "3/7 LTX-2 repo at $Repo (commit $LtxCommit)"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "git not available; install Git and re-run." }
if (-not (Test-Path (Join-Path $Repo ".git"))) {
  & git clone https://github.com/Lightricks/LTX-2 $Repo
  if ($LASTEXITCODE -ne 0) { Die "git clone failed" }
}
$head = (& git -C $Repo rev-parse HEAD 2>$null)
if ($head -and $head.StartsWith($LtxCommit)) {
  Ok "already at $LtxCommit"
} else {
  & git -C $Repo cat-file -e "$LtxCommit^{commit}" 2>$null
  if ($LASTEXITCODE -ne 0) { & git -C $Repo fetch origin }
  & git -C $Repo checkout --quiet $LtxCommit
  if ($LASTEXITCODE -ne 0) { Die "git checkout $LtxCommit failed" }
  Ok "checked out $LtxCommit"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Die "uv not available; install astral-sh.uv and re-run." }
Push-Location $Repo
# --inexact keeps the Windows extras (triton-windows, ninja, nvfp4_cpp) installed by step 5 on re-runs.
& uv sync --extra natten --inexact
$rc = $LASTEXITCODE
Pop-Location
if ($rc -ne 0 -or -not (Test-Path $VenvPy)) { Die "uv sync failed (exit $rc)" }
Ok "venv ready: $VenvPy"

# ----------------------------------------------------------------------------- 4. ltxgen
Step "4/7 ltxgen tools at $LtxGen"
if ($MainPC) {
  $src = "\\$MainPC\ltxgen"
  if (Test-Path $src) {
    & robocopy $src $LtxGen /E /XD .venv __pycache__ /XF *.pyc studio.json studio.db /R:3 /W:5 /NFL /NDL /NP /NJH
    if ($LASTEXITCODE -ge 8) { Die "robocopy from $src failed (exit $LASTEXITCODE)" }
    Ok "copied $src -> $LtxGen"
  } else {
    Warn "cannot reach $src (run deploy\share_models_main_pc.ps1 on the main PC; maybe: net use \\$MainPC\ltxgen /user:$MainPC\<user>)"
  }
}
if (-not (Test-Path (Join-Path $LtxGen "ltxgen.py"))) { Die "$LtxGen\ltxgen.py missing: give -MainPC or copy the ltxgen folder manually." }
if (-not (Test-Path (Join-Path $LtxGen "studio\worker.py"))) { Die "$LtxGen\studio\worker.py missing." }
# The helper scripts hard-code D:\video and CUDA v13.4; adapt the local copies to this PC.
foreach ($f in @("setup_windows_extras.ps1", "build_nvfp4.bat", "build_nvfp4.py")) {
  if ($Root.TrimEnd("\") -ne "D:\video") { Patch-File (Join-Path $LtxGen $f) "D:\video" $Root.TrimEnd("\") }
}
if ($cuda) { Patch-File (Join-Path $LtxGen "build_nvfp4.bat") "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.4" $cuda.FullName }
Ok "ltxgen ready"

# ----------------------------------------------------------------------------- 5. Windows extras
Step "5/7 Windows extras (triton-windows, ltx_kernels, nvfp4_cpp kernel)"
$sp = Join-Path $Repo ".venv\Lib\site-packages"
if (-not (Get-ChildItem "$sp\nvfp4_cpp*.pyd" -ErrorAction SilentlyContinue) -and -not (Find-VCTools)) {
  Warn "VS 2022 Build Tools (C++) not found: the nvfp4_cpp build will fail."
}
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $LtxGen "setup_windows_extras.ps1")
& $VenvPy -c "from ltx_kernels import nvfp4; import sys; sys.exit(0 if nvfp4.is_available() else 1)" 2>$null
if ($LASTEXITCODE -eq 0) { Ok "NVFP4 kernels available" } else { Warn "NVFP4 kernels NOT available: the nvfp4 transformer will fail; use bf16 (-WithQuality) or fix the build (VS Build Tools + CUDA 13.x)." }

# ----------------------------------------------------------------------------- 6. models
Step "6/7 Model files -> $Models (source: $ModelSource)"
$files = @(
  "diffusion_models\ltx-2.5-22b-distilled-transformer-nvfp4.safetensors",
  "text_encoders\gemma4-12b-with-proj-ltx-2.5-bf16.safetensors",
  "vae\ltx-2.5-video-vae-bf16.safetensors",
  "vae\ltx-2.5-video-vae-conv-bf16.safetensors",
  "vae\ltx-2.5-audio-vae-bf16.safetensors",
  "latent_upscale_models\ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
  "model_patches\ltx-2.5-duration-head-bf16.safetensors"
)
$lora = "loras\ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors"
if ($WithQuality) { $files += "diffusion_models\ltx-2.5-22b-distilled-transformer-bf16.safetensors"; $files += $lora }
if ($SkipModels) {
  Info "skipped (-SkipModels)"
} elseif ($ModelSource -eq "smb") {
  if (-not $MainPC) { Die "-ModelSource smb needs -MainPC" }
  $share = "\\$MainPC\ltx-models"
  if (-not (Test-Path $share)) { Die "cannot reach $share. On the main PC run deploy\share_models_main_pc.ps1; if asked for credentials: net use $share /user:$MainPC\<user>" }
  foreach ($f in $files) {
    $dir = Split-Path $f -Parent; $name = Split-Path $f -Leaf
    if (-not (Test-Path (Join-Path $share $f))) { Warn "not on the share: $f"; continue }
    Info "robocopy $f"
    # Unchanged files (same size+time) are skipped, so re-runs are cheap. /Z = restartable on network hiccups.
    & robocopy (Join-Path $share $dir) (Join-Path $Models $dir) $name /MT:16 /Z /R:5 /W:10 /NDL /NJH /NJS
    if ($LASTEXITCODE -ge 8) { Warn "robocopy failed for $f (exit $LASTEXITCODE)" }
  }
} else {
  $env:HF_HUB_DISABLE_XET = "1"   # Xet transfers stalled on this network; plain HTTPS resumes fine
  if (-not (Get-Command hf -ErrorAction SilentlyContinue)) {
    Info "installing the HuggingFace CLI (uv tool install huggingface_hub)"
    & uv tool install "huggingface_hub[cli]"
    Refresh-Path
    $env:Path += ";$env:USERPROFILE\.local\bin"
  }
  & hf auth whoami 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Die "not logged in to HuggingFace. Run 'hf auth login' with an account that accepted the gated licenses of Lightricks/LTX-2.5 and Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler, then re-run."
  }
  foreach ($f in $files) {
    $rel = $f.Replace("\", "/")
    if (Test-Path (Join-Path $Models $f)) { Ok "have $rel"; continue }
    for ($try = 1; $try -le 3; $try++) {
      Info "hf download $rel (try $try)"
      if ($f -eq $lora) {
        & hf download Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler (Split-Path $f -Leaf) --local-dir (Join-Path $Models "loras") 2>&1 | Select-Object -Last 1
      } else {
        & hf download Lightricks/LTX-2.5 $rel --local-dir $Models 2>&1 | Select-Object -Last 1
      }
      if (Test-Path (Join-Path $Models $f)) { break }
    }
  }
}
foreach ($f in $files) {
  $p = Join-Path $Models $f
  if (Test-Path $p) { Ok ("{0} ({1:N1} GB)" -f $f, ((Get-Item $p).Length / 1GB)) } elseif (-not $SkipModels) { Warn "missing $f" }
}

# ----------------------------------------------------------------------------- 7. checks
Step "7/7 ltxgen doctor + worker self-test"
$env:LTXGEN_ROOT = $Root
$py = Find-Python
if (-not $py) { Die "no usable Python found" }
$pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)
$doc = (& $pyExe @pyArgs (Join-Path $LtxGen "ltxgen.py") doctor) -join "`n"
try {
  $d = $doc | ConvertFrom-Json
  Info ("repo={0} venv={1} ffmpeg={2} ltx_kernels={3}" -f $d.repo, $d.venv_python, $d.ffmpeg, $d.ltx_kernels)
  if ($d.torch) { Info ("torch {0} cuda={1} gpu={2} cc={3}" -f $d.torch.torch, $d.torch.cuda, $d.torch.gpu, ($d.torch.cc -join ".")) }
  $miss = @($d.files.PSObject.Properties | Where-Object { -not $_.Value.exists } | ForEach-Object { $_.Name })
  if ($miss.Count) { Info "model files not present: $($miss -join ', ')" }
  if ($d.torch -and $d.torch.cuda) { Ok "doctor: CUDA OK" } else { Warn "doctor: torch cannot see the GPU" }
} catch { Warn "doctor output was not JSON:`n$doc" }

$worker = Join-Path $LtxGen "studio\worker.py"
$tmp = Join-Path $env:TEMP "ltx_worker_selftest"
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
$env:LTX_FAKE_SECONDS = "1"
$fake = (& $pyExe @pyArgs $worker _fake_generate --prompt selftest --preset draft --aspect 16:9 --seconds 1 --out-dir $tmp) -join "`n"
Remove-Item Env:\LTX_FAKE_SECONDS
try { $fr = $fake | ConvertFrom-Json } catch { $fr = $null }
if ($fr -and $fr.ok) { Ok "local dry-run render OK (ffmpeg + subprocess)" } else { Warn "local dry-run render failed: $fake" }

if ($Server -and $Token) {
  # presets=selftest: the server never hands out a real task to this run.
  $out = (& $pyExe @pyArgs $worker --server $Server --token $Token --root $Root --dry-run --once --presets selftest) -join "`n"
  try { $r = $out | ConvertFrom-Json } catch { $r = $null }
  if ($r -and $r.ok) { Ok "server $Server reachable, token accepted, registered as $($r.worker_id)" }
  else { Warn "worker self-test against $Server failed: $out  (firewall on main PC? token? server running?)" }
  $settings = @{ server = $Server; token = $Token; root = $Root } | ConvertTo-Json
  Set-Content -Path (Join-Path $WorkDir "worker_settings.json") -Value $settings -Encoding ascii
  Ok "saved $WorkDir\worker_settings.json (used by start_worker.ps1)"
} else {
  Info "no -Server/-Token given: skipped the server self-test"
}

# Worker presets as auto-detected
$det = & $pyExe @pyArgs -c "import sys; sys.argv=['w']; sys.path.insert(0, r'$LtxGen\studio'); import worker as w; from pathlib import Path; m=w.Models(Path(r'$Root'), w.load_ltxgen(Path(r'$LtxGen\ltxgen.py'))); print(','.join(m.presets()) or 'none')" 2>$null
Info "presets this worker will advertise: $det"

Write-Host ""
if ($script:Problems.Count) {
  Write-Host "Finished with $($script:Problems.Count) warning(s):" -ForegroundColor Yellow
  $script:Problems | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
} else {
  Write-Host "Worker PC ready." -ForegroundColor Green
}
Write-Host "Start the worker:   powershell -ExecutionPolicy Bypass -File `"$LtxGen\studio\deploy\start_worker.ps1`""
Write-Host "Start at logon:     powershell -ExecutionPolicy Bypass -File `"$LtxGen\studio\deploy\install_worker_task.ps1`""
Write-Host "Keep this PC awake: powercfg /change standby-timeout-ac 0"
