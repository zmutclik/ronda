# Ronda — agent (client Windows)

Agent di setiap PC: enroll, heartbeat, metrik, buffer offline. Rancangan lengkap ada di
`../ronda_agent_-_dokumen_pengembangan_client.md`; kontrak API ada di `../server/app/hosts/schemas.py`.

## Status

| Tahap | Isi | Status |
|---|---|---|
| 1 · Fondasi agent | config, enroll, heartbeat 30 dtk, sampler metrik + disk, buffer SQLite, batch metrics, backoff, pemulihan 401 | **Selesai** (teruji di Windows 11, mode console) |
| 2 · Launcher + auto-update | Windows Service, folder per versi, SHA-256 + Ed25519, rollback, `sign_release.py` | **Selesai** (update + rollback crash / tidak sehat teruji di Windows 11, `uji-update.ps1`) |
| 3 · Inventaris + aplikasi | WMI, registry Uninstall + hash, `refresh_inventory` | **Selesai** (kolektor teruji di Windows 11) |
| 4 · Helper screenshot + installer | helper (pipe + mss), supervisor helper di launcher, PyInstaller, Inno Setup | **Selesai** (build + installer + screenshot teruji di Windows 11) |

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

- Mati bawaan: aktif bila `screenshot = true` di `config.toml` (installer `/SCREENSHOT=1`), hanya setelah kebijakan
  disetujui manajemen. Bila mati, permintaan dari server langsung dilaporkan gagal "Screenshot dinonaktifkan di PC ini".
- Launcher (SYSTEM) menjalankan `ronda-helper` di sesi user konsol (`WTSQueryUserToken` + `CreateProcessAsUser`), dari
  folder versi `current`; helper ikut pindah versi/rollback, dihidupkan ulang bila crash (5 → 300 dtk), dan
  dihentikan saat user logoff. Tidak memakai scheduled task.
- Pipe `\\.\pipe\ronda-helper-<sesi>`: auth HMAC dua arah dengan `local.token` (dibuat launcher; diberikan ke helper
  lewat environment), dan agent mengecek exe pemilik pipe ada di `versions\`. Helper mengirim byte JPEG lewat pipe,
  bukan path file. 1600 px / kualitas 70, lalu 1280 px / 50 bila > 3 MB.
- Log helper ada di `%LOCALAPPDATA%\Ronda\logs\helper.log` (folder log Ronda hanya bisa ditulis SYSTEM); galatnya
  juga diteruskan ke agent dan tercatat di `agent.log`.

Development di Linux: `uv run ronda-launcher console --data-dir data` dengan `screenshot = true` menjalankan
`python -m ronda_helper` (socket Unix `data/helper-0.sock`; butuh X untuk mengambil layar).

## Uji di PC Windows

Skrip uji ada di `packaging/uji/` (petunjuk lengkap: `BACA-SAYA-WINDOWS.txt`): `jalankan-test.ps1` (test unit +
kolektor Windows), `jalankan-agent.ps1` (agent mode console, tanpa screenshot), `uji-screenshot.ps1` (build tanpa
paket rilis lalu pasang service dengan `/SCREENSHOT=1 /ALLOWHTTP=1`; butuh Administrator + Inno Setup 6),
`uji-update.ps1` (gerbang Tahap 2: build dengan kunci uji, pasang 1.0.0, lalu update ke 1.0.1, rollback dari 1.0.2 yang
crash saat start dan dari 1.0.3 yang tidak pernah sehat; laporan `hasil-uji-update.txt`). Zip untuk PC uji, dari root
repo:

```bash
git archive --format=zip -o ronda-agent-windows-test.zip HEAD agent
```

## Build & installer (mesin build Windows)

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Version 1.0.0 [-Key D:\kunci-rilis\release-private.pem] [-NoPackage]
#   → dist\ronda-agent-1.0.0.zip (paket auto-update) + dist\ronda-agent-setup-1.0.0.exe (installer)
```

Pasang (manual atau GPO):

```
ronda-agent-setup-1.0.0.exe /VERYSILENT /SUPPRESSMSGBOXES /SERVER=https://ronda.example.local /KEY=IT-xxxxxxxx
    [/CA=\\fileserver\it\ca-rs.pem] [/SCREENSHOT=1] [/ALLOWHTTP=1 (uji saja)] [/LOG=C:\Windows\Temp\ronda-setup.log]
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
# sekali, di mesin build (kunci privat simpan offline, jangan masuk repo/server)
uv run python packaging/sign_release.py keygen --out /media/kunci-rilis
#   → tempel kunci publik ke ronda_agent/release_keys.py SEBELUM build 1.0.0 (tanpa kunci, agent menolak semua update)

# tiap rilis: build PyInstaller → zip → tanda tangan → unggah di menu Pendaftaran Agent › Rilis
uv run python packaging/sign_release.py build --version 1.3.0 --min-launcher 1.0.0 \
    --agent-dir dist/ronda-agent --helper-dir dist/ronda-helper --out ronda-agent-1.3.0.zip
uv run python packaging/sign_release.py sign --key /media/kunci-rilis/release-private.pem ronda-agent-1.3.0.zip
```

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
- File `healthy` ditulis setelah heartbeat pertama berhasil (dipakai launcher di Tahap 2).
- Inventaris (spesifikasi, disk, adaptor) dikirim saat online, tiap 24 jam, dan saat admin menekan Refresh inventaris.
  Daftar aplikasi hanya dikirim bila hash berubah (hash tersimpan di `state.dat`), kecuali diminta admin.
- Test kolektor Windows (WMI, registry) bermarker `windows`: `uv run pytest -m windows` di runner Windows.
