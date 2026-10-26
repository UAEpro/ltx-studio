<#
.SYNOPSIS
  Run ONCE on the main PC, as Administrator. Exposes the model files and ltxgen tools to worker PCs
  (read-only SMB shares) and opens the studio server port on the Private firewall profile only.

  Creates (skips what already exists):
    \\<this PC>\ltx-models  -> <Root>\models\ltx-2.5   read-only
    \\<this PC>\ltxgen      -> <Root>\ltxgen           read-only
    firewall rule "LTX Studio server (TCP <Port>)"     inbound, Private profile only
    enables the built-in "File and Printer Sharing" firewall rules for the Private profile only

  Every command is printed first; nothing changes until you confirm (or pass -Yes).
  Undo:  Remove-SmbShare ltx-models; Remove-SmbShare ltxgen; Remove-NetFirewallRule -DisplayName "LTX Studio server (TCP 7860)"

.PARAMETER ReadAccess  Account(s) given read access to the shares. Default "Everyone"; worker PCs still
                       need valid credentials of a main-PC account (Windows 11 disables guest SMB access).
#>
#Requires -RunAsAdministrator
param(
  [string]$Root = "D:\video",
  [int]$Port = 7860,
  [string[]]$ReadAccess = @("Everyone"),
  [switch]$Yes
)
$ErrorActionPreference = "Stop"
$models = Join-Path $Root "models\ltx-2.5"
$ltxgen = Join-Path $Root "ltxgen"
$ruleName = "LTX Studio server (TCP $Port)"
$fpsGroup = "@FirewallAPI.dll,-28502"   # "File and Printer Sharing" (language-independent id)

foreach ($p in @($models, $ltxgen)) { if (-not (Test-Path $p)) { throw "missing folder: $p" } }

$plan = @()
if (-not (Get-SmbShare -Name "ltx-models" -ErrorAction SilentlyContinue)) {
  $plan += "New-SmbShare -Name 'ltx-models' -Path '$models' -ReadAccess $($ReadAccess -join ',') -Description 'LTX-2.5 model files (read-only)'"
}
if (-not (Get-SmbShare -Name "ltxgen" -ErrorAction SilentlyContinue)) {
  $plan += "New-SmbShare -Name 'ltxgen' -Path '$ltxgen' -ReadAccess $($ReadAccess -join ',') -Description 'ltxgen tools + studio worker (read-only)'"
}
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
  $plan += "New-NetFirewallRule -DisplayName '$ruleName' -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private"
}
$fps = @(Get-NetFirewallRule -Group $fpsGroup -ErrorAction SilentlyContinue |
         Where-Object { $_.Profile -match "Private" -and $_.Enabled -ne "True" -and $_.Direction -eq "Inbound" })
if ($fps.Count) {
  $plan += "Get-NetFirewallRule -Group '$fpsGroup' | Where-Object { `$_.Profile -match 'Private' -and `$_.Direction -eq 'Inbound' } | Enable-NetFirewallRule"
}

Write-Host "Network profile(s):"
Get-NetConnectionProfile | ForEach-Object {
  Write-Host ("  {0} ({1}): {2}" -f $_.Name, $_.InterfaceAlias, $_.NetworkCategory)
  if ($_.NetworkCategory -eq "Public") {
    Write-Host "  [warn] this network is Public: the rules above only apply to Private networks. If this is your home LAN run:" -ForegroundColor Yellow
    Write-Host "         Set-NetConnectionProfile -InterfaceAlias '$($_.InterfaceAlias)' -NetworkCategory Private" -ForegroundColor Yellow
  }
}
if (-not $plan.Count) { Write-Host "Nothing to do: shares and firewall rules already exist." -ForegroundColor Green; exit 0 }

Write-Host ""
Write-Host "Commands to run:" -ForegroundColor Cyan
$plan | ForEach-Object { Write-Host "  $_" }
if (-not $Yes) {
  $ans = Read-Host "Proceed? [y/N]"
  if ($ans -notmatch "^(y|yes)$") { Write-Host "Aborted, nothing changed."; exit 1 }
}

if (-not (Get-SmbShare -Name "ltx-models" -ErrorAction SilentlyContinue)) {
  New-SmbShare -Name "ltx-models" -Path $models -ReadAccess $ReadAccess -Description "LTX-2.5 model files (read-only)" | Out-Null
}
if (-not (Get-SmbShare -Name "ltxgen" -ErrorAction SilentlyContinue)) {
  New-SmbShare -Name "ltxgen" -Path $ltxgen -ReadAccess $ReadAccess -Description "ltxgen tools + studio worker (read-only)" | Out-Null
}
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private | Out-Null
}
if ($fps.Count) { $fps | Enable-NetFirewallRule }

Write-Host ""
Get-SmbShare -Name "ltx-models", "ltxgen" | Format-Table Name, Path, Description -AutoSize
Write-Host "Done. From a worker PC test with:  dir \\$env:COMPUTERNAME\ltx-models" -ForegroundColor Green
Write-Host "If prompted for credentials there: net use \\$env:COMPUTERNAME\ltx-models /user:$env:COMPUTERNAME\<your user>"
