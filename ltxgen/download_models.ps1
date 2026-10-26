# Downloads LTX-2.5 weights one file at a time (multi-file hf runs stalled on this machine).
# Usage: powershell -File download_models.ps1 [-WithBf16]   (bf16 transformer = 42 GB fallback for fp8-cast)
param([switch]$WithBf16)
$ErrorActionPreference = "Continue"
# Xet transfers stalled on multi-GB files here; plain HTTPS writes progressively and resumes.
$env:HF_HUB_DISABLE_XET = "1"
$dir = Join-Path (Split-Path -Parent $PSScriptRoot) "models\ltx-2.5"
$files = @(
  "vae/ltx-2.5-audio-vae-bf16.safetensors",
  "latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
  "vae/ltx-2.5-video-vae-conv-bf16.safetensors",
  "vae/ltx-2.5-video-vae-bf16.safetensors",
  "diffusion_models/ltx-2.5-22b-distilled-transformer-nvfp4.safetensors",
  "text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors"
)
if ($WithBf16) { $files += "diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors" }
foreach ($f in $files) {
  for ($try = 1; $try -le 3; $try++) {
    Write-Output "[$(Get-Date -f HH:mm:ss)] $f (try $try)"
    hf download Lightricks/LTX-2.5 $f --local-dir $dir 2>&1 | Select-Object -Last 1
    if (Test-Path (Join-Path $dir $f)) { break }
  }
}
hf download Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors --local-dir "$dir\loras" 2>&1 | Select-Object -Last 1
Write-Output "[$(Get-Date -f HH:mm:ss)] DONE"
