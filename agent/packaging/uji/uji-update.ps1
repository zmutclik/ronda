# Uji gerbang Tahap 2 di VM Windows: auto-update + rollback dengan paket yang sengaja rusak.
#
#   1. Build dengan KUNCI UJI (dibuat di uji-kunci\, bukan kunci rilis produksi):
#        installer <dasar>  +  paket <baik> (normal), <crash> (crash saat start), <macet> (jalan tapi tidak pernah sehat)
#   2. Copot instalasi Ronda lama (bila ada), pasang installer <dasar>, set update_jitter_sec = 0.
#   3. Per tahap: Anda unggah + mulai rilis di server, skrip me-restart service lalu memantau launcher:
#        <baik>  → current pindah ke <baik>, masa uji selesai (update_status installed)
#        <crash> → crash 3x dalam 5 menit → kembali ke <baik>, <crash> masuk bad_versions (rollback)
#        <macet> → tanpa file healthy 5 menit → kembali ke <baik>, <macet> masuk bad_versions (rollback)
#   4. Laporan: hasil-uji-update.txt (kirim file ini).
#
# Pakai (PowerShell "Run as Administrator", di folder agent\):
#   powershell -ExecutionPolicy Bypass -File .\packaging\uji\uji-update.ps1 -Key IT-xxxxxxxx
# Ulang tanpa build: tambah -LewatiBuild. Ulang setelah rilis sudah diunggah ke server: pakai nomor versi baru, mis.
#   -Baik 1.0.4 -Crash 1.0.5 -Macet 1.0.6   (server menolak versi yang sama diunggah dua kali)
# Butuh: uv dan Inno Setup 6 (winget install JRSoftware.InnoSetup).
# Setelah uji, VM ini memercayai kunci uji: copot Ronda Agent sebelum VM dipakai untuk hal lain.
param(
    [string]$Server = "http://192.168.80.10:8034",
    [Parameter(Mandatory = $true)][string]$Key,
    [string]$Baik = "1.0.1",
    [string]$Crash = "1.0.2",
    [string]$Macet = "1.0.3",
    [switch]$LewatiBuild
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))   # root agent/
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host "Jalankan PowerShell sebagai Administrator."; exit 1 }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv belum terpasang. Jalankan: winget install astral-sh.uv  (lalu buka PowerShell baru)"; exit 1
}

$Data = "C:\ProgramData\Ronda"
$Out = "dist\uji-update"
$KeyDir = "uji-kunci"
$Pem = "$KeyDir\release-private.pem"
$Report = "hasil-uji-update.txt"
$Dasar = (uv run python -c "import ronda_agent; print(ronda_agent.__version__)").Trim()
$Patched = @("ronda_agent\__init__.py", "ronda_agent\release_keys.py", "packaging\entry\agent.py")

function Step($text) { Write-Host ""; Write-Host "== $text ==" -ForegroundColor Cyan }

function Find-Iscc {
    $iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
    if (-not $iscc) {
        $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") |
            Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $iscc) { throw "ISCC.exe (Inno Setup 6) tidak ditemukan: winget install JRSoftware.InnoSetup" }
    return $iscc
}

function Set-Entry($mode) {
    $entry = switch ($mode) {
        "crash" { "import ronda_agent.main  # noqa: F401  (paket tetap berisi agent lengkap)`nraise RuntimeError(`"UJI: rilis ini sengaja crash saat start`")`n" }
        "macet" { "import time`n`nimport ronda_agent.main  # noqa: F401`n`nwhile True:          # UJI: jalan terus tanpa heartbeat, tidak pernah menulis healthy`n    time.sleep(3600)`n" }
        default { $null }
    }
    if ($entry) { Set-Content packaging\entry\agent.py $entry -Encoding ascii -NoNewline }
    else { Copy-Item "$Out\asli\agent.py" packaging\entry\agent.py -Force }
}

function Build-Version($version, $mode) {
    Step "Build $version ($mode)"
    Set-Content ronda_agent\__init__.py "__version__ = `"$version`"`n" -Encoding ascii -NoNewline
    Set-Entry $mode
    uv run pyinstaller --noconfirm --clean packaging/ronda.spec
    if ($LASTEXITCODE) { throw "PyInstaller gagal ($version)" }
    if ($mode -eq "dasar") {
        & (Find-Iscc) "/DAppVersion=$version" packaging/installer.iss
        if ($LASTEXITCODE) { throw "Inno Setup gagal" }
        Copy-Item "dist\ronda-agent-setup-$version.exe" $Out -Force
        return
    }
    $zip = "$Out\ronda-agent-$version.zip"
    uv run python packaging/sign_release.py build --version $version --min-launcher $Dasar `
        --agent-dir dist/ronda-agent --helper-dir dist/ronda-helper --out $zip
    if ($LASTEXITCODE) { throw "pembuatan paket $version gagal" }
    $signed = uv run python packaging/sign_release.py sign --key $Pem $zip
    if ($LASTEXITCODE) { throw "tanda tangan $version gagal" }
    Set-Content "$zip.sig" ($signed | Select-Object -Last 1).Trim() -Encoding ascii
}

function Build-All {
    New-Item -ItemType Directory -Force "$Out\asli", $KeyDir | Out-Null
    if (-not (Test-Path $Pem)) {
        uv run python packaging/sign_release.py keygen --out $KeyDir --no-passphrase | Out-Null
        if ($LASTEXITCODE) { throw "keygen gagal" }
    }
    $pub = (uv run python -c @"
from cryptography.hazmat.primitives import serialization
import sys; sys.path.insert(0, 'packaging')
from sign_release import public_key_b64
print(public_key_b64(serialization.load_pem_private_key(open(r'$Pem', 'rb').read(), None)))
"@).Trim()
    uv sync --group build
    if ($LASTEXITCODE) { throw "uv sync gagal" }
    foreach ($f in $Patched) { Copy-Item $f "$Out\asli\" -Force }
    try {
        Set-Content ronda_agent\release_keys.py "# KUNCI UJI (uji-update.ps1), bukan kunci rilis produksi`nPUBLIC_KEYS: tuple[str, ...] = (`"$pub`",)`n" -Encoding ascii -NoNewline
        Build-Version $Dasar "dasar"
        Build-Version $Baik "baik"
        Build-Version $Crash "crash"
        Build-Version $Macet "macet"
    } finally {
        foreach ($f in $Patched) { Copy-Item "$Out\asli\$(Split-Path -Leaf $f)" $f -Force }
        Write-Host "File sumber dipulihkan."
    }
}

function Read-State {
    $current = if (Test-Path "$Data\current") { (Get-Content "$Data\current" -Raw).Trim() } else { "" }
    $launcher = if (Test-Path "$Data\launcher.json") { Get-Content "$Data\launcher.json" -Raw | ConvertFrom-Json } else { $null }
    $proc = Get-Process ronda-agent -ErrorAction SilentlyContinue | Select-Object -First 1
    $running = if ($proc -and $proc.Path) { Split-Path -Leaf (Split-Path -Parent (Split-Path -Parent $proc.Path)) } else { "-" }
    [pscustomobject]@{
        Current = $current
        Status  = if ($launcher) { $launcher.update_status } else { $null }
        Trial   = if ($launcher -and $launcher.trial) { $launcher.trial.version } else { $null }
        Bad     = if ($launcher -and $launcher.bad_versions) { @($launcher.bad_versions) } else { @() }
        Healthy = Test-Path "$Data\healthy"
        Running = $running
    }
}

function Format-State($s) {
    "current=$($s.Current) jalan=$($s.Running) status=$($s.Status) uji=$($s.Trial) healthy=$($s.Healthy) bad=[$($s.Bad -join ',')]"
}

$Results = New-Object System.Collections.Generic.List[string]

function Test-Phase($version, $expect, $timeoutMin) {
    $zip = (Resolve-Path "$Out\ronda-agent-$version.zip").Path
    $sig = (Get-Content "$Out\ronda-agent-$version.zip.sig" -Raw).Trim()
    Set-Clipboard $sig
    Step "Tahap: rilis $version ($expect)"
    Write-Host "Di browser: $Server > Pendaftaran Agent > Rilis"
    Write-Host "  1. Unggah file : $zip"
    Write-Host "     Versi       : $version"
    Write-Host "     Tanda tangan: (sudah disalin ke clipboard, tinggal Ctrl+V)"
    Write-Host "     $sig"
    Write-Host "  2. Klik Mulai. PC baru ada di Ring 2: Naikkan ke Ring 1 lalu Ring 2"
    Write-Host "     (atau minta ring PC ini diubah ke 0 di database)."
    Read-Host "Tekan Enter setelah rilis $version berjalan"
    Restart-Service RondaLauncher          # agent langsung cek /update saat start
    $start = Get-Date
    $last = ""
    $shown = 0
    $ok = $false
    while (((Get-Date) - $start).TotalMinutes -lt $timeoutMin) {
        $s = Read-State
        $line = Format-State $s
        $sec = ((Get-Date) - $start).TotalSeconds
        if ($line -ne $last) { Write-Host ("[{0,4:N0} dtk] {1}" -f $sec, $line); $last = $line; $shown = $sec }
        elseif ($sec - $shown -ge 30) {
            Write-Host ("[{0,4:N0} dtk] masih menunggu (masa uji tanpa healthy maks 300 dtk, batas tahap $timeoutMin mnt)" -f $sec)
            $shown = $sec
        }
        if ($expect -eq "terpasang") {
            $ok = $s.Current -eq $version -and -not $s.Trial -and $s.Status -eq "installed" -and $s.Running -eq $version
        } else {
            $ok = $s.Current -eq $Baik -and $s.Bad -contains $version -and $s.Status -eq "rollback" -and $s.Running -eq $Baik
        }
        if ($ok) { break }
        Start-Sleep -Seconds 3
    }
    $elapsed = [int]((Get-Date) - $start).TotalSeconds
    $verdict = if ($ok) { "LULUS" } else { "GAGAL" }
    $msg = "$verdict  $version harus $expect  ($elapsed dtk)  $(Format-State (Read-State))"
    Write-Host $msg -ForegroundColor $(if ($ok) { "Green" } else { "Red" })
    $Results.Add($msg)
    if ($ok -and $expect -eq "rollback") {
        Write-Host "Cek di server: halaman PC menampilkan event rollback v$version, rilis menghitung 1 rollback."
    }
}

# ---------- 1. build ----------
if (-not $LewatiBuild) { Build-All }
foreach ($v in @($Baik, $Crash, $Macet)) {
    if (-not (Test-Path "$Out\ronda-agent-$v.zip.sig")) { throw "$Out\ronda-agent-$v.zip belum dibuild (jalankan tanpa -LewatiBuild)" }
}

# ---------- 2. pasang versi dasar ----------
Step "Pasang $Dasar"
$uninstaller = "$env:ProgramFiles\Ronda\unins000.exe"
if (Test-Path $uninstaller) {
    Write-Host "Mencopot instalasi lama (state, config, versi lama dihapus; PC perlu disetujui ulang) ..."
    Start-Process $uninstaller -Wait -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES"
    for ($i = 0; $i -lt 60 -and ((Test-Path $uninstaller) -or (Get-Service RondaLauncher -ErrorAction SilentlyContinue)); $i++) {
        Start-Sleep -Seconds 2          # uninstaller Inno menyalin diri ke %TEMP% dan bisa selesai belakangan
    }
    if (Get-Service RondaLauncher -ErrorAction SilentlyContinue) { throw "service RondaLauncher masih ada setelah uninstall" }
}
foreach ($f in @("current", "launcher.json", "pending_switch", "healthy")) { Remove-Item "$Data\$f" -Force -ErrorAction SilentlyContinue }
$p = Start-Process "$Out\ronda-agent-setup-$Dasar.exe" -Wait -PassThru -ArgumentList @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SERVER=$Server", "/KEY=$Key", "/ALLOWHTTP=1", "/LOG=$PWD\ronda-setup.log")
if ($p.ExitCode) { Write-Host "Installer gagal (kode $($p.ExitCode)); lihat ronda-setup.log"; exit 1 }
Stop-Service RondaLauncher
$cfg = Get-Content "$Data\config.toml" | Where-Object { $_ -notmatch '^\s*update_jitter_sec\s*=' }
Set-Content "$Data\config.toml" (@($cfg) + "update_jitter_sec = 0   # uji-update.ps1: unduh langsung, tanpa jeda acak") -Encoding UTF8
Start-Service RondaLauncher
Start-Sleep -Seconds 10
Write-Host (Format-State (Read-State))
Write-Host "Setujui PC ini di $Server > Pendaftaran Agent (agent hanya cek update bila PC sudah disetujui)."
Read-Host "Tekan Enter setelah PC disetujui"

# ---------- 3. update + rollback ----------
Test-Phase $Baik "terpasang" 10
Test-Phase $Crash "rollback" 10
Test-Phase $Macet "rollback" 12        # masa uji 5 menit tanpa healthy

# ---------- 4. laporan ----------
$lulus = ($Results | Where-Object { $_ -like "LULUS*" }).Count -eq 3
$text = @("Uji auto-update + rollback Ronda  $(Get-Date -Format s)  $env:COMPUTERNAME",
    "OS: $((Get-CimInstance Win32_OperatingSystem).Caption) $((Get-CimInstance Win32_OperatingSystem).BuildNumber)",
    "Hasil: $(if ($lulus) { 'LULUS' } else { 'GAGAL' })", "") + $Results + @("",
    "== launcher.json ==", (Get-Content "$Data\launcher.json" -Raw), "",
    "== versions\ ==", ((Get-ChildItem "$Data\versions" -Directory).Name -join ", "), "",
    "== launcher.log (100 baris terakhir) ==") + (Get-Content "$Data\logs\launcher.log" -Tail 100) + @("",
    "== agent.log (60 baris terakhir) ==") + (Get-Content "$Data\logs\agent.log" -Tail 60)
Set-Content $Report $text -Encoding UTF8
Write-Host ""
Write-Host "Hasil: $(if ($lulus) { 'LULUS' } else { 'GAGAL' }). Laporan: $PWD\$Report" -ForegroundColor $(if ($lulus) { "Green" } else { "Red" })
Write-Host "Hentikan rilis $Macet di server. Copot Ronda Agent dari VM ini setelah uji (VM memercayai kunci uji)."
