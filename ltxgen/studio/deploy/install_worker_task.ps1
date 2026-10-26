<#
.SYNOPSIS
  Registers (or with -Uninstall removes) the Scheduled Task "LTX Studio Worker", which runs
  start_worker.ps1 at logon of the current user (interactive session, so the GPU driver is available).
  The PC must be logged in (enable auto-logon or stay signed in) and must not sleep:
    powercfg /change standby-timeout-ac 0
  Run in the user's own PowerShell (no admin needed for a per-user logon task).
#>
param(
  [string]$Root = "D:\video",
  [string]$TaskName = "LTX Studio Worker",
  [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "Removed task '$TaskName' (if it existed)."
  exit 0
}
$script = Join-Path $PSScriptRoot "start_worker.ps1"
if (-not (Test-Path (Join-Path $Root "studio_worker\worker_settings.json"))) {
  Write-Host "[warn] $Root\studio_worker\worker_settings.json missing: run setup_worker_pc.ps1 with -Server/-Token first." -ForegroundColor Yellow
}
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$script`" -Root `"$Root`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Write-Host "Registering task '$TaskName': at logon of $user -> powershell -File `"$script`" -Root `"$Root`""
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
  -Description "LTX Studio render worker (pulls shots from the studio server)" -Force | Out-Null
Write-Host "Done. Start now without logging off:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove:                               powershell -File `"$PSCommandPath`" -Uninstall"
