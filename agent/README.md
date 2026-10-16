# Ronda — agent (client Windows)

Agent di setiap PC: enroll, heartbeat, metrik, buffer offline. Rancangan lengkap ada di
`../ronda_agent_-_dokumen_pengembangan_client.md`; kontrak API ada di `../server/app/hosts/schemas.py`.

## Status

| Tahap | Isi | Status |
|---|---|---|
| 1 · Fondasi agent | config, enroll, heartbeat 30 dtk, sampler metrik + disk, buffer SQLite, batch metrics, backoff, pemulihan 401 | **Selesai** (teruji di Windows 11, mode console) |
| 2 · Launcher + auto-update | Windows Service, folder per versi, SHA-256 + Ed25519, rollback, `sign_release.py` | **Selesai** (update + rollback crash / tidak sehat teruji di Windows 11, `uji-update.ps1`) |
| 3 · Inventaris + aplikasi | WMI, registry Uninstall + hash, `refresh_inventory` | **Selesai** (kolektor teruji di Windows 11) |
| 4 · Helper screenshot + installer | helper (pipe + mss), supervisor helper di launcher, PyInstaller, Inno Setup | **Selesai** (build + installer + screenshot teruji di Windows 11; jendela pemberitahuan ke user belum, `BACA-SAYA-WINDOWS.txt` 5c) |

## Development

```bash
uv sync
mkdir -p data && cat > data/config.toml <<'EOF'
server_url = "http://127.0.0.1:8000"
enrollment_key = "IT-xxxxxxxx"     # buat di menu Pendaftaran Agent
EOF
uv run python -m ronda_agent --data-dir data --console
```

Di Windows `--data-dir` bawaannya `C:\ProgramData\Ronda`, dan `state.dat` dienkripsi DPAPI (scope mesin).
Di Linux `state.dat` disimpan tanpa enkripsi (hanya untuk development).

## Test

```bash
uv run pytest                     # unit + alur agent (server palsu) + launcher (proses sungguhan, masa uji detik)
TEST_DATABASE_URL="mysql+asyncmy://ronda:ronda@127.0.0.1:33061/ronda_test" uv run pytest   # + kontrak
uv run ruff check .
```

Test kontrak menjalankan server asli (`../server/.venv`, uvicorn + migrasi) dan memakai MariaDB test yang sama dengan
test server (lihat `../server/README.md`). Database `_test` dikosongkan; jangan jalankan bersamaan dengan test server.

## Screenshot (helper)

- Aktif bawaan (kebijakan screenshot disetujui manajemen, 2026-10-16). Matikan per PC dengan installer
  `/SCREENSHOT=0` (`screenshot = false` di `config.toml`); permintaan dari server lalu langsung dilaporkan gagal
  "Screenshot dinonaktifkan di PC ini".
- Pemberitahuan ke user: bila Admin mencentang "Beri notifikasi ke user" (perintah `notify`, bawaan ya), helper
  menampilkan jendela kecil di pojok kanan bawah selama 10 detik **setelah** gambar diambil (tidak ikut terekam):
  "Tim IT mengambil gambar layar komputer ini pukul HH:MM…". Jendela Win32 biasa (`ronda_helper/notify.py`), bukan
  toast: tidak disembunyikan Focus Assist, tidak merebut fokus keyboard, hilang saat diklik. Tidak muncul bila
  pengambilan gambar gagal. Belum diuji di Windows (langkah 5c di `BACA-SAYA-WINDOWS.txt`).
- Launcher (SYSTEM) menjalankan `ronda-helper` di sesi user konsol (`WTSQueryUserToken` + `CreateProcessAsUser`), dari
  folder versi `current`; helper ikut pindah versi/rollback, dihidupkan ulang bila crash (5 → 300 dtk), dan
  dihentikan saat user logoff. Tidak memakai scheduled task.
- Pipe `\\.\pipe\ronda-helper-<sesi>`: auth HMAC dua arah dengan `local.token` (dibuat launcher; diberikan ke helper
  lewat environment), dan agent mengecek exe pemilik pipe ada di `versions\`. Helper mengirim byte JPEG lewat pipe,
  bukan path file. 1600 px / kualitas 70, lalu 1280 px / 50 bila > 3 MB.
- Log helper ada di `%LOCALAPPDATA%\Ronda\logs\helper.log` (folder log Ronda hanya bisa ditulis SYSTEM); galatnya
  juga diteruskan ke agent dan tercatat di `agent.log`.

Development di Linux: `uv run ronda-launcher console --data-dir data` menjalankan
`python -m ronda_helper` (socket Unix `data/helper-0.sock`; butuh X untuk mengambil layar).

## Uji di PC Windows

Skrip uji ada di `packaging/uji/` (petunjuk lengkap: `BACA-SAYA-WINDOWS.txt`): `jalankan-test.ps1` (test unit +
kolektor Windows), `jalankan-agent.ps1` (agent mode console, tanpa screenshot), uji cepat jendela pemberitahuan
screenshot (langkah 5c), `uji-screenshot.ps1` (build tanpa paket rilis lalu pasang service dengan `/ALLOWHTTP=1`;
butuh Administrator + Inno Setup 6),
`uji-update.ps1` (gerbang Tahap 2: build dengan kunci uji, pasang 1.0.0, lalu update ke 1.0.1, rollback dari 1.0.2 yang
crash saat start dan dari 1.0.3 yang tidak pernah sehat; laporan `hasil-uji-update.txt`). Zip untuk PC uji, dari root
repo:

```bash
git archive --format=zip -o ronda-agent-windows-test.zip HEAD agent
```

## Build & installer (mesin build Windows)

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Version 1.0.0 [-Key E:\release-utama.pem] [-NoPackage]
#   → dist\ronda-agent-1.0.0.zip (paket auto-update) + dist\ronda-agent-setup-1.0.0.exe (installer)
```

`build.ps1` juga menjalankan `packaging/licenses.py`: teks lisensi Ronda, Python, PyInstaller (bootloader), dan semua
dependency runtime dikumpulkan ke `THIRD_PARTY_LICENSES.txt` di tiap folder program, jadi ikut installer dan paket
auto-update. Build gagal bila ada paket tanpa file lisensi.

Pasang (manual atau GPO):

```
ronda-agent-setup-1.0.0.exe /VERYSILENT /SUPPRESSMSGBOXES /SERVER=https://ronda.example.local /KEY=IT-xxxxxxxx
    [/CA=\\fileserver\it\ca-rs.pem] [/SCREENSHOT=0 (matikan screenshot)] [/ALLOWHTTP=1 (uji saja)] [/LOG=C:\Windows\Temp\ronda-setup.log]
```

- Instalasi baru wajib `/SERVER` + `/KEY`. Instalasi ulang mempertahankan `config.toml`; parameter yang diberikan
  menimpa nilainya. Versi installer yang lebih baru dari `current` dipasang lewat `pending_switch` (masa uji +
  rollback yang sama dengan auto-update); versi yang lebih lama tidak menurunkan versi aktif.
- Hasil: service `RondaLauncher` (auto, LocalSystem, restart 1 menit 3×) di `C:\Program Files\Ronda\ronda-launcher\`,
  versi awal di `C:\ProgramData\Ronda\versions\<versi>\`, ACL data dir SYSTEM + Administrators (Users baca/eksekusi
  `versions\`).
- Uninstall: hentikan + hapus service, hapus semua kecuali `logs\`.

## Rilis & auto-update

```bash
# tiap rilis (Windows, USB kunci utama terpasang sebagai E:): build PyInstaller → zip → tanda tangan
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Version 1.3.0 -Key E:\release-utama.pem
# → unggah dist\ronda-agent-1.3.0.zip + signature di menu Pendaftaran Agent › Rilis, lalu cabut USB
```

### Kunci tanda tangan rilis

Agent hanya memasang paket yang ditandatangani kunci yang tertanam di `ronda_agent/release_keys.py`. Siapa pun yang
memegang kunci privat **dan** akses admin server bisa menjalankan program sebagai SYSTEM di semua PC; kunci yang
hilang tanpa cadangan berarti semua PC harus dipasang ulang manual. Karena itu: dua kunci, terenkripsi passphrase,
offline.

Sekali, sebelum build 1.0.0 (di mesin build, dengan 2 USB). **Belum dilakukan**: `PUBLIC_KEYS` masih kosong, jadi
`build.ps1` tanpa `-NoPackage` (paket rilis) ditolak sampai kunci diisi.

```powershell
uv run python packaging/sign_release.py keygen --name utama    --out E:\   # passphrase ≥ 12 karakter
uv run python packaging/sign_release.py keygen --name cadangan --out F:\   # passphrase lain
```

1. Tempel kedua kunci publik yang dicetak ke `PUBLIC_KEYS` di `ronda_agent/release_keys.py`, commit (kunci publik
   aman di repo). `sign_release.py build` memperingatkan bila hanya ada 1 kunci.
2. USB **utama**: dipegang penanggung jawab rilis. USB **cadangan**: brankas di lokasi lain, dipakai hanya bila
   utama hilang/rusak. Passphrase tidak disimpan bersama USB-nya (password manager / amplop tersegel).
3. Cek USB kapan saja: `sign_release.py pubkey --key E:\release-utama.pem` → harus "tertanam di release_keys.py".

`sign` menolak kunci yang tidak tertanam. Passphrase diminta saat `sign`; untuk otomatisasi isi env
`RONDA_RELEASE_PASSPHRASE`. `keygen --no-passphrase` hanya untuk uji (`uji-update.ps1`).

Kunci utama hilang atau bocor: tanda tangani rilis berikutnya dengan **cadangan**; di rilis itu `release_keys.py`
membuang kunci utama dan menambah kunci cadangan baru (keygen di USB baru). Setelah semua PC memakai rilis itu,
kunci lama tidak berlaku lagi. Bila bocor, segera ganti juga password admin server (paket hanya bisa masuk lewat
menu Rilis).

Alur: agent cek `/update` saat online dan tiap 4 jam → jeda acak 0–30 menit (`waiting`) → unduh (`downloading`) →
verifikasi ukuran, SHA-256, Ed25519 atas byte zip → ekstrak ke `versions\<versi>\` → tulis `pending_switch` → keluar
kode 75. Launcher memindah `current`, menjalankan versi baru dalam masa uji: crash 3× dalam 5 menit atau tanpa
`healthy` dalam 5 menit → kembali ke versi lama, versi itu masuk `bad_versions` (tidak dicoba lagi) dan server melihat
`rollback`. Setelah stabil 24 jam, folder versi selain current + previous dihapus. Status bersama ada di `launcher.json`.

Launcher development: `uv run ronda-launcher console --data-dir data` (di Windows tanpa argumen = service).

## Perilaku penting

- Semua sampel masuk `buffer.db` dulu; baris dihapus hanya setelah server membalas 200. Buffer dibatasi 2 hari /
  6.000 baris per tabel.
- PC `pending`: hanya heartbeat; metrik tetap dikumpulkan dan dikirim begitu server membalas `online`.
- 401: berhenti kirim, cek `config.toml` tiap 1 jam; bila ada `enrollment_key` baru, enroll ulang.
- 422 pada batch metrik: batch dibuang (tidak diulang). 5xx/timeout: backoff 5 → 300 dtk ±20%.
- File `healthy` ditulis setelah heartbeat pertama berhasil (dipakai launcher untuk masa uji versi baru).
- Inventaris (spesifikasi, disk, adaptor) dikirim saat online, tiap 24 jam, dan saat admin menekan Refresh inventaris.
  Daftar aplikasi hanya dikirim bila hash berubah (hash tersimpan di `state.dat`), kecuali diminta admin.
- Test kolektor Windows (WMI, registry) bermarker `windows`: `uv run pytest -m windows` di runner Windows.
