# Build rilis di mesin build Windows: PyInstaller → zip rilis (auto-update) → installer (Inno Setup).
#
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Version 1.0.0
#   powershell ... -Version 1.3.0 -MinLauncher 1.0.0 -Key E:\release-utama.pem   # passphrase diminta
#   powershell ... -Version 1.0.0 -NoPackage    # uji saja: installer tanpa paket rilis (release_keys.py boleh kosong)
#   powershell ... -Version 1.1.0 -UltraVnc C:\build\UltraVNC_1830.zip   # zip UltraVNC, atau folder berisi winvnc.exe
#
# Butuh: uv, Inno Setup 6 (iscc.exe di PATH atau lokasi bawaan). Versi harus sama dengan ronda_agent/__init__.py.
# UltraVNC: unduh zip dari https://uvnc.com (bukan setup). -UltraVnc boleh zip-nya langsung (diekstrak sementara;
# bila ada winvnc.exe x86 dan x64, yang x64 dipakai) atau folder yang berisi winvnc.exe. Tanpa -UltraVnc installer tetap jadi, tapi remote di PC itu dilaporkan "tidak terpasang".
# UltraVNC hanya ikut installer, tidak ikut paket rilis (auto-update).
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$MinLauncher = "1.0.0",
    [string]$Key,
    [string]$UltraVnc,
    [switch]$NoPackage
)
$ErrorActionPreference = "Stop"
if ($UltraVnc) { $UltraVnc = (Resolve-Path $UltraVnc).Path }   # path relatif terhadap folder pemanggil, sebelum pindah
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

# UltraVNC → dist/uvnc (installer: {app}\uvnc). Catatan lisensi GPL selalu ikut.
Remove-Item -Recurse -Force dist/uvnc -ErrorAction SilentlyContinue
New-Item -ItemType Directory dist/uvnc | Out-Null
Copy-Item packaging/uvnc-NOTICE.txt dist/uvnc/NOTICE-RONDA.txt
if ($UltraVnc) {
    $source = $UltraVnc
    if ($UltraVnc -like "*.zip") {
        $source = "dist/uvnc-zip"
        Remove-Item -Recurse -Force $source -ErrorAction SilentlyContinue
        Expand-Archive -Path $UltraVnc -DestinationPath $source
    }
    # winvnc.exe bisa di akar atau di subfolder (mis. x64\ dan x86\ / Win32\): pilih yang 64-bit
    $root = (Resolve-Path $source).Path
    $exes = @(Get-ChildItem $root -Recurse -Filter winvnc.exe)
    if (-not $exes) { throw "winvnc.exe tidak ada di $UltraVnc" }
    # nama subfolder saja (bukan path penuh: "UltraVNC_1640" juga mengandung "64")
    $exe = $exes | Where-Object { $_.DirectoryName.Substring($root.Length) -match '(?i)(x64|amd64|win64)' } |
        Select-Object -First 1
    if (-not $exe) {
        if ($exes.Count -gt 1) { throw "Ada beberapa winvnc.exe di $UltraVnc dan tidak ada yang bertanda x64; arahkan -UltraVnc ke folder x64" }
        $exe = $exes[0]
    }
    Write-Host "UltraVNC dari $($exe.DirectoryName)"
    Copy-Item (Join-Path $exe.DirectoryName "*") dist/uvnc -Recurse -Exclude "ultravnc.ini"
    if ($source -eq "dist/uvnc-zip") { Remove-Item -Recurse -Force $source }
    $uvncVersion = (Get-Item dist/uvnc/winvnc.exe).VersionInfo.FileVersion
    $uvncHash = (Get-FileHash dist/uvnc/winvnc.exe -Algorithm SHA256).Hash
    Write-Host "UltraVNC $uvncVersion, winvnc.exe SHA-256 $uvncHash"
} else {
    Write-Warning "Tanpa -UltraVnc: installer tidak membawa UltraVNC (remote tidak tersedia)"
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
