# Test agent Ronda di Windows: kolektor Windows (WMI, registry) lalu seluruh test unit.
# Pakai: powershell -ExecutionPolicy Bypass -File .\packaging\uji\jalankan-test.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))   # root agent/
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv belum terpasang. Jalankan: winget install astral-sh.uv  (lalu buka PowerShell baru)"; exit 1
}
uv sync
Write-Host "== Test kolektor Windows (WMI, registry) =="
uv run pytest -m windows -v
$win = $LASTEXITCODE
Write-Host "== Semua test unit (kontrak dilewati tanpa TEST_DATABASE_URL) =="
uv run pytest -v -p no:cacheprovider 2>&1 | Tee-Object -FilePath hasil-test.txt
Write-Host "Hasil lengkap tersimpan di hasil-test.txt"
if ($win -ne 0 -or $LASTEXITCODE -ne 0) { exit 1 }
