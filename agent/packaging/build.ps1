# Build rilis di mesin build Windows: PyInstaller → zip rilis (auto-update) → installer (Inno Setup).
#
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Version 1.0.0
#   powershell ... -Version 1.3.0 -MinLauncher 1.0.0 -Key E:\release-utama.pem   # passphrase diminta
#   powershell ... -Version 1.0.0 -NoPackage    # uji saja: installer tanpa paket rilis (release_keys.py boleh kosong)
#
# Butuh: uv, Inno Setup 6 (iscc.exe di PATH atau lokasi bawaan). Versi harus sama dengan ronda_agent/__init__.py.
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$MinLauncher = "1.0.0",
    [string]$Key,
    [switch]$NoPackage
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$codeVersion = (uv run python -c "import ronda_agent; print(ronda_agent.__version__)").Trim()
if ($codeVersion -ne $Version) { throw "Versi $Version tidak sama dengan ronda_agent.__version__ ($codeVersion)" }

uv sync --group build
if ($LASTEXITCODE) { throw "uv sync gagal" }
uv run pyinstaller --noconfirm --clean packaging/ronda.spec
if ($LASTEXITCODE) { throw "PyInstaller gagal" }

# Teks lisensi Python + dependency yang ikut di-bundle: disalin ke tiap program, jadi ikut installer dan paket rilis
$licenses = "dist/THIRD_PARTY_LICENSES.txt"
uv run python packaging/licenses.py --pyinstaller --out $licenses
if ($LASTEXITCODE) { throw "pengumpulan lisensi gagal" }
foreach ($program in "ronda-agent", "ronda-helper", "ronda-launcher") {
    Copy-Item $licenses "dist/$program/THIRD_PARTY_LICENSES.txt"
}

$zip = "dist/ronda-agent-$Version.zip"
if (-not $NoPackage) {
    uv run python packaging/sign_release.py build --version $Version --min-launcher $MinLauncher `
        --agent-dir dist/ronda-agent --helper-dir dist/ronda-helper --out $zip
    if ($LASTEXITCODE) { throw "pembuatan paket rilis gagal" }
    if ($Key) {
        uv run python packaging/sign_release.py sign --key $Key $zip
        if ($LASTEXITCODE) { throw "tanda tangan gagal" }
    }
}

$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { throw "ISCC.exe (Inno Setup 6) tidak ditemukan: winget install JRSoftware.InnoSetup" }
}
& $iscc "/DAppVersion=$Version" packaging/installer.iss
if ($LASTEXITCODE) { throw "Inno Setup gagal" }

Write-Host "Selesai:"
if (-not $NoPackage) { Write-Host "  paket rilis : $zip  (unggah di Pendaftaran Agent > Rilis)" }
Write-Host "  installer   : dist/ronda-agent-setup-$Version.exe"
