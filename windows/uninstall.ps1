#Requires -Version 5.1
<#
  מסיר את טימי לווינדוס. ההקלטות והסיכומים נשארים (מסמכים\טימי - הקלטות, או התיקייה שנבחרה).
    powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\Timmy\windows\uninstall.ps1" [-AppDir <תיקייה>] [-KeepModel]
  מוחק: ההפעלה עם הכניסה למחשב, חיבור הוואטסאפ במנהל האישורים, קיצורי הדרך, ואת התיקייה של טימי
  (%LOCALAPPDATA%\Timmy: הסביבה, המודל, ההגדרות והצ'אטים). -KeepModel משאיר את המודל (התקנה מחדש מהירה).
#>
[CmdletBinding()]
param(
    [string]$AppDir = "",
    [switch]$KeepModel
)
$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
$DefaultAppDir = Join-Path $env:LOCALAPPDATA "Timmy"
if (-not $AppDir) { $AppDir = $DefaultAppDir }
if ($AppDir -ne $DefaultAppDir) { $env:TIMMY_APP_DIR = $AppDir }
$Py = Join-Path $AppDir "venv\Scripts\python.exe"

Write-Host "==> סוגר את טימי"
$venvLike = (Join-Path $AppDir "venv").ToLower()
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.ToLower().StartsWith($venvLike) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

Write-Host "==> מוחק הגדרות מערכת"
if (Test-Path $Py) { & $Py -m timmy.install_helper cleanup | Out-Null }
else { Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "Timmy" -ErrorAction SilentlyContinue }
foreach ($dir in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
    Remove-Item (Join-Path $dir "טימי.lnk") -ErrorAction SilentlyContinue
}

Write-Host "==> מוחק את הקבצים של טימי"
if ($KeepModel -and (Test-Path (Join-Path $AppDir "models"))) {
    Get-ChildItem $AppDir -Force | Where-Object { $_.Name -ne "models" } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
} else {
    Remove-Item $AppDir -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "✓ טימי הוסר. ההקלטות נשארו."
exit 0
