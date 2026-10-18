# Jalankan agent Ronda di Windows (mode console, untuk uji).
# Pakai: powershell -ExecutionPolicy Bypass -File .\packaging\uji\jalankan-agent.ps1 -Key IT-xxxxxxxx
param(
    [string]$Server = "http://192.168.80.10:8034",
    [string]$Key = ""
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))   # root agent/
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv belum terpasang. Jalankan: winget install astral-sh.uv  (lalu buka PowerShell baru)"; exit 1
}
uv sync
New-Item -ItemType Directory -Force data | Out-Null
if (-not (Test-Path data\config.toml)) {
    if (-not $Key) { Write-Host "Belum ada data\config.toml. Ulangi dengan -Key IT-xxxxxxxx (dari menu Pendaftaran Agent)"; exit 1 }
    "server_url = `"$Server`"`nenrollment_key = `"$Key`"`n" | Out-File data\config.toml -Encoding ascii
}
uv run python -m ronda_agent --data-dir data --console
