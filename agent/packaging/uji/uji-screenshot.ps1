# Uji screenshot di Windows: build exe (PyInstaller) + installer, lalu pasang sebagai service (screenshot aktif bawaan).
# Helper screenshot hanya bisa diuji lewat instalasi ini: launcher (service SYSTEM) yang menjalankan helper di sesi
# user, dan agent hanya menerima helper yang exe-nya ada di C:\ProgramData\Ronda\versions\.
# Pakai (PowerShell "Run as Administrator"):
#   powershell -ExecutionPolicy Bypass -File .\packaging\uji\uji-screenshot.ps1 -Key IT-xxxxxxxx
# Butuh: uv dan Inno Setup 6 (winget install JRSoftware.InnoSetup).
param(
    [string]$Server = "http://192.168.80.10:8034",
    [Parameter(Mandatory = $true)][string]$Key
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))   # root agent/
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host "Jalankan PowerShell sebagai Administrator."; exit 1 }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv belum terpasang. Jalankan: winget install astral-sh.uv  (lalu buka PowerShell baru)"; exit 1
}
$version = (uv run python -c "import ronda_agent; print(ronda_agent.__version__)").Trim()
& .\packaging\build.ps1 -Version $version -NoPackage   # tanpa paket rilis: release_keys.py masih kosong
if ($LASTEXITCODE) { exit 1 }
$setup = "dist\ronda-agent-setup-$version.exe"
Write-Host "Memasang $setup ..."
$p = Start-Process $setup -Wait -PassThru -ArgumentList @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SERVER=$Server", "/KEY=$Key", "/ALLOWHTTP=1",
    "/LOG=$PWD\ronda-setup.log")
if ($p.ExitCode) { Write-Host "Installer gagal (kode $($p.ExitCode)); lihat ronda-setup.log"; exit 1 }
Get-Service RondaLauncher
Write-Host "Terpasang. Log: C:\ProgramData\Ronda\logs\ (agent.log, launcher.log), helper: %LOCALAPPDATA%\Ronda\logs\helper.log"
