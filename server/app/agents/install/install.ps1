# Ronda Agent: pemasang Windows 10/11 64-bit. Dibuat server __SERVER__.
# Jalankan di PowerShell "Run as Administrator":
#
#   $env:RONDA_KEY='<enrollment key>'; irm __SERVER__/install.ps1 | iex
#
# Opsional sebelum `irm`: $env:RONDA_CA='\\fileserver\it\ca.pem' (CA internal), $env:RONDA_SCREENSHOT='0',
# $env:RONDA_REMOTE='0'. Skrip mengunduh installer dari server, mencocokkan SHA-256, lalu menjalankan
# ronda-agent-setup.exe /VERYSILENT /SERVER=... /KEY=... (sama dengan instalasi GPO). Instal ulang/upgrade tanpa
# RONDA_KEY: config.toml lama dipertahankan.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'      # bilah progres membuat Invoke-WebRequest PS 5.1 sangat lambat

    $Server = '__SERVER__'
    $PackageUrl = '__PACKAGE_URL__'
    $PackageSha256 = '__PACKAGE_SHA256__'
    $PackageVersion = '__PACKAGE_VERSION__'
    $ServerError = '__SERVER_ERROR__'

    function Say([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
    # throw, bukan exit: lewat `irm | iex` exit menutup jendela PowerShell pemakai
    function Fail([string]$Message) { throw "GAGAL: $Message" }

    $key = "$env:RONDA_KEY".Trim()
    $ca = "$env:RONDA_CA".Trim()
    $screenshot = "$env:RONDA_SCREENSHOT".Trim()
    $remote = "$env:RONDA_REMOTE".Trim()
    # Key tidak dibiarkan tertinggal di environment sesi PowerShell ini
    Remove-Item Env:RONDA_KEY -ErrorAction SilentlyContinue

    if ($ServerError) { Fail $ServerError }
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Fail 'jalankan PowerShell sebagai Administrator (klik kanan > Run as Administrator).'
    }
    if (-not [Environment]::Is64BitOperatingSystem) { Fail 'Ronda Agent hanya untuk Windows 64-bit.' }
    if ($key -and $key -notmatch '^[A-Za-z0-9-]{4,64}$') { Fail 'enrollment key berisi karakter tidak valid.' }
    if ($ca -and -not (Test-Path -LiteralPath $ca -PathType Leaf)) { Fail "file CA $ca tidak ada." }
    foreach ($flag in @(@('RONDA_SCREENSHOT', $screenshot), @('RONDA_REMOTE', $remote))) {
        if ($flag[1] -and $flag[1] -notin @('0', '1')) { Fail "$($flag[0]) harus 0 atau 1." }
    }
    $installed = [bool](Get-Service -Name RondaLauncher -ErrorAction SilentlyContinue)
    if (-not $installed -and -not $key) {
        Fail ("instalasi baru butuh enrollment key: `$env:RONDA_KEY='<key>'; irm $Server/install.ps1 | iex " +
              '(key dari menu Pendaftaran Agent).')
    }

    # PowerShell 5.1 bawaan Windows 10 belum tentu memakai TLS 1.2
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor
        [Net.SecurityProtocolType]::Tls12

    $work = Join-Path ([IO.Path]::GetTempPath()) ("ronda-install-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null
    try {
        $setup = Join-Path $work "ronda-agent-setup-$PackageVersion.exe"
        Say "Mengunduh Ronda Agent $PackageVersion"
        try {
            Invoke-WebRequest -Uri $PackageUrl -OutFile $setup -UseBasicParsing
        } catch {
            Fail "unduhan gagal: $PackageUrl ($($_.Exception.Message))"
        }
        $hash = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne $PackageSha256) { Fail 'SHA-256 installer tidak cocok (installer diganti saat mengunduh?); jalankan lagi.' }

        $log = Join-Path $env:windir 'Temp\ronda-setup.log'
        $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/SERVER=$Server", "`"/LOG=$log`"")
        if ($key) { $arguments += "/KEY=$key" }
        if ($ca) { $arguments += "`"/CA=$ca`"" }
        if ($screenshot) { $arguments += "/SCREENSHOT=$screenshot" }
        if ($remote) { $arguments += "/REMOTE=$remote" }
        if ($Server.StartsWith('http://')) {
            Write-Warning 'server memakai http:// (tanpa TLS); hanya untuk uji.'
            $arguments += '/ALLOWHTTP=1'
        }

        Say 'Menjalankan installer (beberapa detik sampai 1 menit)'
        $process = Start-Process -FilePath $setup -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -ne 0) { Fail "installer keluar dengan kode $($process.ExitCode); lihat $log" }

        $service = Get-Service -Name RondaLauncher -ErrorAction SilentlyContinue
        if (-not $service) { Fail "service RondaLauncher tidak terpasang; lihat $log" }
        Say "Selesai: Ronda Agent $PackageVersion terpasang (service RondaLauncher: $($service.Status))."
        if (-not $installed) { Write-Host '    PC ini akan muncul di Pendaftaran Agent > Persetujuan PC baru; setujui di sana.' }
        Write-Host '    Log: C:\ProgramData\Ronda\logs\'
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}
