# Re-applies the Windows-specific additions to the LTX-2 venv. Run after any `uv sync`
# (or use `uv sync --inexact`, which keeps them).
#   - triton-windows (fast DiffVAE decode kernels) and ninja (extension build)
#   - ltx_kernels importable via .pth (upstream setup.py is Linux-only)
#   - all2all_cpp stub (multi-GPU only extension, Linux-only)
#   - nvfp4_cpp built for RTX 50xx (sm_120a) with MSVC
$ErrorActionPreference = "Stop"
$repo = Join-Path (Split-Path -Parent $PSScriptRoot) "LTX-2"
$sp = "$repo\.venv\Lib\site-packages"
Push-Location $repo
uv pip install triton-windows ninja
Pop-Location
Set-Content "$sp\ltx_kernels_src.pth" "$repo\packages\ltx-kernels\src" -Encoding ascii
Copy-Item "$PSScriptRoot\all2all_cpp_stub.py" "$sp\all2all_cpp.py" -Force
if (-not (Get-ChildItem "$sp\nvfp4_cpp*.pyd" -ErrorAction SilentlyContinue)) {
  cmd /c "$PSScriptRoot\build_nvfp4.bat"
}
& "$repo\.venv\Scripts\python.exe" -c "from ltx_kernels import nvfp4; import triton; print('nvfp4', nvfp4.is_available(), 'triton', triton.__version__)"
