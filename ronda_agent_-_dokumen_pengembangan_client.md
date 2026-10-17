# Ronda Agent — Dokumen Pengembangan Client

Oct 16, 2026 · @Fahrudin Hariadi

## Ringkasan

Client Ronda adalah agent Windows yang dipasang di setiap PC rumah sakit. Agent mengirim kondisi PC ke server Ronda dan menjalankan perintah terbatas dari server. Server (`server/`) sudah selesai dan teruji (97 test lulus per 16 Okt 2026); dokumen ini menjabarkan apa yang dibangun di sisi client agar cocok dengan kontrak API server tersebut. Client (`agent/`) sudah selesai Tahap 1–4; status per tahap ada di bagian Tahapan.

**Target client**

- Windows 10/11 64-bit, berjalan otomatis saat PC menyala dan pulih sendiri bila crash.
- Ringan: CPU di bawah 1% dan RAM di bawah 50 MB saat idle.
- Tidak membuka port masuk; semua koneksi dimulai dari client lewat HTTPS.
- Bisa diperbarui terpusat dari server sejak versi pertama yang disebar.

**Di luar lingkup**

- Remote control (noVNC), deployment software, client Linux/macOS.
- WebSocket `/ws/agent`: server belum menyediakannya. Perintah diambil dari respons heartbeat.

**Rujukan**

- Kontrak payload: `server/app/hosts/schemas.py`.
- Endpoint agent: `server/app/agents/router.py`.
- Contoh pemakaian API: `server/scripts/simulate_agents.py`.
- Dokumen induk: `rorda_-_dokumen_pengembangan.md`.

## Arsitektur client

Client terdiri dari tiga proses, karena Windows Service berjalan di Session 0 dan tidak bisa melihat desktop user, dan karena `.exe` yang sedang berjalan tidak bisa ditimpa saat update.

Hanya agent yang bicara ke server; helper hanya melayani agent.

```
PC Windows
  ronda-launcher   (Windows Service: pilih versi, update, rollback)
        |
        | menjalankan
        v
  ronda-agent      <-- named pipe -->   ronda-helper (sesi user: screenshot)
        |
        | HTTPS, selalu dimulai dari client;
        | respons heartbeat membawa perintah
        v
Server Ronda       (/api/agent/*, FastAPI + MariaDB, sudah jadi di server/)
```

| Proses | Jalan sebagai | Tugas | Sering berubah? |
| --- | --- | --- | --- |
| `ronda-launcher` | Windows Service (auto-start, restart on failure), LocalSystem | Menjalankan agent dari folder versi aktif, memasang update, rollback | Sangat jarang |
| `ronda-agent` | Proses anak launcher, LocalSystem | Enroll, heartbeat, metrik, inventaris, aplikasi, menjalankan perintah, cek update | Tiap rilis |
| `ronda-helper` | Dijalankan launcher di sesi user konsol (`CreateProcessAsUser`), hak user biasa | Mengambil screenshot layar saat diminta agent | Tiap rilis |

**Komunikasi lokal agent ↔ helper**

- Named pipe `\\.\pipe\ronda-helper-<sesi>`, satu per sesi Windows; agent memakai sesi konsol aktif. Pipe dibuat dengan `FILE_FLAG_FIRST_PIPE_INSTANCE`, dan agent mengecek exe pemilik pipe berada di `versions\`.
- Autentikasi: challenge HMAC dua arah (`multiprocessing.connection`) dengan token acak di `C:\ProgramData\Ronda\local.token` (dibuat launcher, hanya bisa dibaca SYSTEM dan Administrators). Token tidak pernah lewat pipe; helper menerimanya lewat environment saat dijalankan launcher, bukan argumen.
- Pesan JSON: `{"op":"screenshot","request_id":"…","max_width":1600,"max_bytes":3145728,"notify":true}` → header JSON (`ok`, `content_type`, `width`, `height` atau `error`), lalu byte JPEG. Helper mengirim byte, bukan path file, agar agent (SYSTEM) tidak membaca file yang ditunjuk proses user.
- Bila tidak ada user login atau helper tidak menjawab dalam 15 detik, agent melapor gagal ke server.
- Launcher menjaga helper: dijalankan saat ada user di konsol dan screenshot tidak dimatikan (`screenshot = false`), dihentikan saat logoff, dijalankan ulang dari versi baru saat update/rollback, dihidupkan lagi bila crash (jeda 5 → 300 dtk).

**Bahasa dan pustaka**

| Kebutuhan | Pilihan |
| --- | --- |
| Bahasa | Python 3.13, satu bahasa dengan server |
| Metrik | psutil |
| HTTP | httpx (sync atau async, HTTP/1.1, verifikasi TLS ke CA internal RS) |
| Spesifikasi hardware | WMI (`wmi` / `pywin32`) |
| Aplikasi terpasang | `winreg` bawaan Python |
| Screenshot | mss + Pillow (resize + JPEG) |
| Verifikasi paket update | `cryptography` (Ed25519) |
| Buffer offline | `sqlite3` bawaan Python |
| Service Windows | pywin32 (`win32serviceutil`) untuk launcher |
| Packaging | PyInstaller (onedir) per proses |
| Installer | Inno Setup, mendukung `/VERYSILENT` untuk GPO |

## Kontrak API server

Semua endpoint di bawah sudah ada di server dan menjadi kontrak yang wajib diikuti agent. Semua body JSON UTF-8, timestamp ISO 8601 **dengan zona waktu** (mis. `2026-10-16T03:40:00Z`); timestamp tanpa zona ditolak 422. Field yang tidak dikenal diabaikan server.

| Method | Endpoint | Auth | Syarat status | Fungsi |
| --- | --- | --- | --- | --- |
| POST | `/api/agent/enroll` | enrollment key di body | — | Daftar pertama kali, mendapat `agent_token` |
| POST | `/api/agent/heartbeat` | Bearer token | pending atau approved | Tanda hidup + kondisi terakhir, menerima `commands` |
| POST | `/api/agent/metrics` | Bearer token | approved | Batch sampel metrik (maks 500 sampel + 500 disk) |
| PUT | `/api/agent/inventory` | Bearer token | approved | Spesifikasi hardware, disk, adaptor jaringan |
| PUT | `/api/agent/apps` | Bearer token | approved | Daftar aplikasi terpasang + hash (maks 2000) |
| POST | `/api/agent/screenshots/{request_id}` | Bearer token | approved | Upload gambar mentah (maks 3 MB) |
| POST | `/api/agent/screenshots/{request_id}/fail` | Bearer token | approved | Lapor screenshot gagal |
| GET | `/api/agent/update` | Bearer token | approved | Cek rilis yang harus dipasang |
| GET | `/api/agent/releases/{versi}/download` | Bearer token | approved | Unduh paket zip |

**Kode respons yang harus ditangani**

| Kode | Arti | Tindakan agent |
| --- | --- | --- |
| 200 | Berhasil | Lanjut |
| 401 | Token tidak ada, salah, atau PC ditolak/diblokir | Berhenti kirim data, log error, coba enroll ulang hanya bila ada enrollment key baru di config |
| 403 | Enrollment key tidak valid, atau PC masih pending | Saat enroll: berhenti dan log. Saat kirim data: tahan di buffer, tetap heartbeat |
| 404 | Screenshot/rilis tidak ditemukan | Buang permintaan itu |
| 413 | File terlalu besar | Lapor gagal "Gambar melebihi batas ukuran server" (helper sudah mengompres ke ≤ 3 MB sebelum upload) |
| 422 | Payload tidak valid | Log isi error, buang batch itu (jangan diulang terus) |
| 5xx / timeout | Server bermasalah | Simpan di buffer, ulangi dengan backoff |

**Enroll**

```json
POST /api/agent/enroll
{"enrollment_key": "IT-xxxxxxxx", "hostname": "IGD-PC03", "agent_version": "1.0.0",
 "ip": "10.10.12.31", "serial_number": "PF2ABC12"}
→ {"host_id": 42, "agent_token": "<rahasia>", "status": "pending"}
```

Token diterbitkan langsung saat enroll walau status masih `pending`. Status `online` berarti PC langsung disetujui karena IP-nya masuk subnet auto-approve. Selama pending, server hanya menerima heartbeat.

**Heartbeat**

```json
POST /api/agent/heartbeat
{"ts": "2026-10-16T03:40:00Z", "agent_version": "1.0.0", "update_status": "none",
 "ip": "10.10.12.31", "cpu_pct": 12.5, "ram_pct": 61.0,
 "disks": [{"mount": "C:", "used_pct": 71.2}], "latency_ms": 3.4,
 "logged_user": "perawat.igd1", "boot_at": "2026-10-16T00:12:00Z"}
→ {"status": "online", "commands": [{"type": "screenshot", "request_id": "7f3c…", "notify": true}]}
```

`update_status` hanya boleh `none`, `waiting`, `downloading`, `installed`, `rollback`, atau `failed`. Maksimal 26 disk.

**Metrik (batch)**

```json
POST /api/agent/metrics
{"samples": [{"ts": "…Z", "cpu_pct": 12.5, "ram_pct": 61.0, "latency_ms": 3.4,
              "net_in_bps": 120000, "net_out_bps": 40000}],
 "disks":   [{"ts": "…Z", "mount": "C:", "used_pct": 71.2}]}
→ {"stored": 1}
```

Server membuang sampel yang lebih tua dari 2 hari atau lebih dari 5 menit di depan jam server. Sampel dengan `ts` sama boleh dikirim ulang; server menimpanya (idempoten).

**Inventaris**

```json
PUT /api/agent/inventory
{"os_name": "Windows 11 Pro", "os_build": "22631.4317", "cpu_model": "Intel Core i5-10500",
 "cpu_cores": 6, "cpu_threads": 12, "ram_total_mb": 8192, "manufacturer": "Dell",
 "model": "OptiPlex 3080", "serial_number": "PF2ABC12",
 "disks": [{"mount": "C:", "label": "OS", "media": "SSD", "total_bytes": 256060514304, "used_bytes": 182000000000}],
 "adapters": [{"name": "Ethernet", "description": "Realtek PCIe GbE", "mac": "00:1A:2B:3C:4D:5E",
               "is_virtual": false, "is_up": true, "is_server_route": true, "speed_mbps": 1000,
               "ipv4": "10.10.12.31", "prefix_len": 24, "gateway": "10.10.12.1", "dns": "10.10.1.10",
               "dhcp": true, "rx_bps": 120000, "tx_bps": 40000}]}
```

`is_server_route` = adaptor yang dipakai untuk menuju server. Untuk Wi-Fi isi juga `ssid` dan `signal_pct`. Maksimal 26 disk dan 32 adaptor.

**Aplikasi**

```json
PUT /api/agent/apps
{"hash": "<sha256 hex 64 karakter>", "apps": [{"name": "Google Chrome", "version": "129.0.6668",
  "publisher": "Google LLC", "install_date": "2026-09-01"}]}
→ {"changed": true}
```

Hash dihitung dari daftar yang sudah diurutkan (lihat bagian Data). Server tidak mengubah apa pun bila hash sama.

**Screenshot**

- Upload: body = byte gambar JPEG/PNG/WebP apa adanya (bukan multipart), header `Content-Type: image/jpeg`, `X-Width`, `X-Height`.
- Gagal: `POST …/fail` dengan `{"error": "Tidak ada user login"}` (1–200 karakter).

**Update**

```json
GET /api/agent/update
→ {"update": null}
→ {"update": {"version": "1.3.0", "sha256": "…", "signature": "<base64>", "size": 18234567,
               "url": "/api/agent/releases/1.3.0/download"}}
```

Server hanya menawarkan rilis berstatus `rolling` yang ring-nya sudah mencakup ring PC ini dan versinya lebih baru.

## Data yang dikumpulkan

Agent menjalankan beberapa loop dengan interval berbeda; heartbeat 30 detik menjaga PC tetap online karena server menandai offline setelah 120 detik tanpa heartbeat.

| Data | Sumber | Interval | Dikirim lewat |
| --- | --- | --- | --- |
| CPU % total | `psutil.cpu_percent(interval=None)` | sampel tiap 30 dtk | heartbeat + metrics |
| RAM % | `psutil.virtual_memory().percent` | sampel tiap 30 dtk | heartbeat + metrics |
| Network in/out (bps) | selisih `psutil.net_io_counters()` dibagi detik | sampel tiap 30 dtk | metrics |
| Latency ke server (ms) | TCP connect ke host:port server | tiap 60 dtk | heartbeat + metrics |
| Disk % per partisi tetap | `psutil.disk_usage` untuk `disk_partitions(all=False)` tipe fixed | tiap 5 menit | heartbeat + metrics.disks |
| User login | `psutil.users()` (sesi konsol aktif) | tiap heartbeat | heartbeat |
| Waktu boot | `psutil.boot_time()` | saat start | heartbeat |
| IP utama | IP lokal socket yang tersambung ke server | tiap heartbeat | heartbeat |
| Spesifikasi OS, CPU, RAM, merek, model, serial | `platform`, WMI `Win32_ComputerSystem`, `Win32_BIOS`, `Win32_Processor` | saat start, tiap 24 jam, dan saat diminta | inventory |
| Disk fisik (label, SSD/HDD, kapasitas) | WMI `MSFT_PhysicalDisk` / `Win32_LogicalDisk` | sama dengan spesifikasi | inventory |
| Adaptor jaringan (MAC, kecepatan, gateway, DNS, DHCP, Wi-Fi) | `psutil.net_if_addrs/net_if_stats`, WMI `Win32_NetworkAdapterConfiguration`, `netsh wlan show interfaces` | sama dengan spesifikasi | inventory |
| Aplikasi terpasang | Registry `HKLM\...\Uninstall`, `HKLM\WOW6432Node\...\Uninstall`, `HKU\<SID>\...\Uninstall` | tiap 24 jam dan saat start; kirim hanya bila hash berubah | apps |

**Aturan pengumpulan**

- Semua timestamp diambil dalam UTC (`datetime.now(UTC)`), bukan jam lokal.
- Sampel disimpan ke buffer lokal, lalu dikirim sebagai batch `metrics` tiap 5 menit (10 sampel per batch dalam kondisi normal).
- Nilai persen dibulatkan 1 desimal dan dijepit ke 0–100 sebelum dikirim.
- String dipotong sesuai batas server (mis. hostname 63, nama aplikasi 255, versi aplikasi 60, publisher 150) agar tidak ditolak 422.
- Aplikasi: lewati entri tanpa `DisplayName`, entri `SystemComponent=1`, dan entri dengan `ParentKeyName` (update/patch). Hapus duplikat berdasarkan (nama, versi).
- Hash aplikasi = SHA-256 dari JSON daftar aplikasi yang sudah diurutkan per (nama, versi), `sort_keys=True`, tanpa spasi. `install_date` dari format registry `YYYYMMDD` diubah ke `YYYY-MM-DD`, atau `null` bila tidak valid.

## Perintah dari server

Server menitipkan perintah di respons heartbeat (`commands`), sehingga jeda perintah paling lama satu interval heartbeat (30 detik). Agent hanya mengenal dua perintah; tipe lain diabaikan dan dicatat di log. Tidak ada eksekusi perintah bebas.

| Perintah | Isi | Tindakan agent |
| --- | --- | --- |
| `screenshot` | `request_id`, `notify` | Minta helper mengambil layar → upload ke `/screenshots/{request_id}`, atau lapor ke `/fail` |
| `refresh_inventory` | — | Kirim ulang `inventory` dan `apps` (apps tetap dengan hash; server mengabaikan bila sama) |

**Alur screenshot**

1. Agent menerima `{"type":"screenshot","request_id":"…"}` di heartbeat. Simpan `request_id` yang sedang diproses agar heartbeat berikutnya tidak memprosesnya dua kali (server terus mengulang perintah selama statusnya masih `requested`, maksimal 10 menit).
2. Tidak ada user login atau helper tidak jalan → `POST /fail` dengan alasan singkat.
3. Helper mengambil layar monitor utama dengan mss, memperkecil ke lebar maks 1600 px, JPEG kualitas 70 (di memori, tanpa file sementara).
4. Bila hasil di atas 3 MB, ulangi dengan kualitas 50 dan lebar 1280 px.
5. Agent mengunggah byte gambar dengan header `Content-Type`, `X-Width`, `X-Height` (di thread sendiri; heartbeat tetap jalan, satu screenshot sekaligus).
6. Respons 404/422 berarti permintaan sudah kedaluwarsa atau selesai: hapus dari daftar proses, jangan diulang.

Pemberitahuan ke user: bila `notify` bernilai true (Admin mencentang "Beri notifikasi ke user"; field tidak ada = true), helper menampilkan jendela kecil di pojok kanan bawah selama 10 detik setelah gambar diambil. Jendela Win32 tanpa fokus, bukan toast (toast bisa disembunyikan Focus Assist).

## Enrollment, konfigurasi, dan keandalan

Agent menyimpan identitasnya sekali saat enroll, lalu bekerja dari buffer lokal agar data tidak hilang saat jaringan atau server putus.

**Alur enrollment**

1. Installer menulis `server_url` dan `enrollment_key` ke `config.toml`.
2. Agent start tanpa `agent_token` → `POST /enroll`.
3. Respons berisi token → simpan ke `state.dat` (dienkripsi DPAPI scope mesin), hapus `enrollment_key` dari config.
4. Status `pending` → kirim heartbeat saja tiap 30 detik sampai respons berubah menjadi `online`; metrik tetap dikumpulkan ke buffer.
5. Status `online` → kirim inventory, apps, lalu kosongkan buffer metrik.
6. 401 di kemudian hari (token dicabut/PC ditolak) → berhenti kirim, log `ERROR`, cek ulang tiap 1 jam apakah ada enrollment key baru di config.

**File di `C:\ProgramData\Ronda\`**

| File/folder | Isi | ACL |
| --- | --- | --- |
| `config.toml` | `server_url`, `enrollment_key` (sementara), interval, `ca_bundle`, `screenshot` (bawaan `true`; `/SCREENSHOT=0` mematikan) | SYSTEM + Administrators |
| `state.dat` | `agent_token` terenkripsi DPAPI, `host_id`, hash aplikasi terakhir | SYSTEM |
| `local.token` | Token pipe agent ↔ helper | SYSTEM + Administrators |
| `buffer.db` | SQLite: antrean sampel metrik dan disk | SYSTEM |
| `versions\<versi>\` | Agent + helper per versi | SYSTEM tulis, Users baca/eksekusi |
| `current` | Nama versi aktif | SYSTEM |
| `logs\` | Log berputar | SYSTEM + Administrators |

Contoh `config.toml`:

```toml
server_url = "https://ronda.example.local"
enrollment_key = "IT-xxxxxxxx"
ca_bundle = "C:\\ProgramData\\Ronda\\ca-rs.pem"
heartbeat_sec = 30
metrics_batch_sec = 300
disk_sec = 300
inventory_hours = 24
```

**Buffer offline dan retry**

- Setiap sampel masuk `buffer.db` dulu; batch dikirim lalu baris dihapus hanya setelah respons 200.
- Buffer dibatasi 2 hari (sesuai batas server) atau 6.000 baris; yang tertua dibuang lebih dulu.
- Kirim ulang aman karena server menimpa sampel dengan `ts` sama.
- Gagal jaringan/5xx: exponential backoff 5 dtk → 10 → 20 … maks 5 menit, ditambah jitter acak ±20%.
- Heartbeat tidak di-buffer: yang penting hanya yang terbaru.
- Timeout HTTP: connect 5 dtk, baca 15 dtk; upload screenshot dan unduh rilis 120 dtk.

**TLS**

- Verifikasi sertifikat selalu aktif, memakai CA internal RS dari `ca_bundle`. Tidak ada opsi `verify=False` di build produksi.
- Pinning sertifikat opsional, bila tim IT memutuskan.

**Logging**

- Rotating file 5 MB × 5 file di `logs\agent.log`, `logs\launcher.log`; helper (hak user, tidak bisa menulis `logs\`) di `%LOCALAPPDATA%\Ronda\logs\helper.log`, dan galatnya juga tercatat di `agent.log`.
- Level bawaan `INFO`; token, enrollment key, dan isi screenshot tidak pernah ditulis ke log.
- Format satu baris: waktu UTC, level, modul, pesan.

## Auto-update

Launcher memasang versi baru ke folder terpisah dan kembali ke versi lama bila versi baru gagal; mekanisme ini wajib ada di versi 1.0.0 karena agent tanpa updater hanya bisa diperbarui manual.

**Alur**

1. Agent cek `GET /api/agent/update` tiap 4 jam dan saat start. Respons `null` → tidak ada yang dilakukan.
2. Ada rilis → lapor `update_status: "waiting"`, tunggu jeda acak 0–30 menit agar ratusan PC tidak mengunduh bersamaan.
3. Lapor `downloading`, unduh zip ke `versions\_incoming\`.
4. Agent memverifikasi ukuran = `size`, SHA-256 = `sha256`, dan tanda tangan Ed25519 dengan kunci publik yang tertanam di `ronda_agent/release_keys.py` (utama + cadangan). Gagal → hapus file, lapor `failed`.
5. Ekstrak ke `versions\<versi>\`, tulis file penanda `pending_switch` berisi versi baru, lalu agent keluar dengan kode 75.
6. Launcher melihat penanda, mengganti `current` ke versi baru, menjalankan agent baru, lalu menjalankan ulang helper di sesi user.
7. Agent baru lapor `installed` dengan `agent_version` baru di heartbeat pertama.
8. Bila agent baru crash 3 kali dalam 5 menit atau tidak menulis file sehat (`healthy`) dalam 5 menit, launcher mengembalikan `current` ke versi sebelumnya; agent lama lapor `rollback`.
9. Setelah versi baru stabil 24 jam, launcher menghapus versi selain dua yang terakhir.

**Keputusan yang perlu dipakai bersama tim build**

- Tanda tangan dibuat atas **byte zip mentah** dengan kunci privat Ed25519 di mesin build, dikodekan base64, lalu ditempel di form Rilis. Server hanya menyimpannya; verifikasi dilakukan agent sebelum menulis `pending_switch`.
- Kunci privat tidak pernah masuk repo atau server produksi.
- Isi zip: `ronda-agent\`, `ronda-helper\` (hasil PyInstaller onedir) dan `manifest.json` (`version`, `min_launcher`).
- Bila `min_launcher` lebih tinggi dari launcher terpasang, agent menolak paket itu (folder versi dihapus, lapor `failed`). Launcher hanya diperbarui lewat installer baru (`/VERYSILENT`, manual atau GPO).

**Health check lokal**

Agent menulis `healthy` setelah heartbeat pertama berhasil (status 200). Launcher memakai file ini, bukan respons server langsung, agar launcher tetap sederhana dan tanpa jaringan.

## Struktur repo, packaging, dan testing

Kode client tinggal di `agent/` di samping `server/`, dengan inti logika yang bisa dites di Linux dan lapisan Windows yang tipis.

```
agent/
├── pyproject.toml          # uv, Python 3.13
├── ronda_agent/
│   ├── main.py             # loop utama: heartbeat, sampler, batch, jadwal
│   ├── config.py           # baca config.toml + state.dat
│   ├── api.py              # klien httpx, satu fungsi per endpoint, mapping kode respons
│   ├── schemas.py          # salinan batas field dari server/app/hosts/schemas.py
│   ├── buffer.py           # SQLite antrean sampel
│   ├── collect/
│   │   ├── metrics.py      # psutil
│   │   ├── inventory.py    # WMI
│   │   └── apps.py         # winreg + hash
│   ├── commands.py         # screenshot, refresh_inventory
│   ├── updater.py          # cek, unduh, verifikasi, ekstrak
│   ├── release.py          # tata letak folder versi, verifikasi paket, manifest
│   ├── release_keys.py     # kunci publik Ed25519 yang dipercaya (utama + cadangan)
│   └── pipe.py             # klien named pipe ke helper
├── ronda_helper/           # main.py: server named pipe; capture.py: mss + JPEG; notify.py: pemberitahuan ke user
├── ronda_launcher/         # main.py + service.py: Windows Service; core.py: pemilih versi, masa uji, rollback;
│                           # helper.py + winsession.py: menjalankan helper di sesi user
├── packaging/
│   ├── ronda.spec          # PyInstaller: agent, helper, launcher (entry/ berisi titik masuk tiap exe)
│   ├── build.ps1           # build lengkap di mesin build Windows
│   ├── installer.iss       # Inno Setup
│   ├── sign_release.py     # keygen, build paket, tanda tangan Ed25519 zip rilis
│   ├── licenses.py         # THIRD_PARTY_LICENSES.txt: lisensi Python + dependency yang di-bundle
│   └── uji/                # skrip uji di PC Windows (BACA-SAYA-WINDOWS.txt)
└── tests/
```

**Installer (Inno Setup)**

- Parameter: `/SERVER=https://… /KEY=IT-xxxx /VERYSILENT`, agar bisa disebar lewat GPO atau dijalankan manual.
- Memasang launcher sebagai service `RondaLauncher` (start otomatis, recovery: restart setelah 1 menit, tiga kali).
- Tidak ada scheduled task: helper dijalankan launcher di sesi user (lihat Arsitektur client).
- Membuat/memperbarui `config.toml` (`/SCREENSHOT=0` mematikan screenshot, bawaan aktif), menyalin CA RS (`/CA=`), dan mengatur ACL folder. `local.token` dibuat launcher saat pertama jalan.
- Instalasi ulang dengan versi lebih baru dari `current` dipasang lewat `pending_switch` (masa uji + rollback); versi lebih lama tidak menurunkan versi aktif.
- Uninstall: hentikan dan hapus service, hapus folder kecuali `logs\`.

**Testing**

| Lapis | Cara | Jalan di |
| --- | --- | --- |
| Unit: hash aplikasi, pemotongan string, buffer, backoff, verifikasi tanda tangan | pytest | Linux/CI |
| Kontrak API | pytest terhadap server Ronda asli (docker MariaDB `_test`, seperti test server) | Linux/CI |
| Kolektor Windows (WMI, registry, psutil) | pytest dengan marker `windows` | runner Windows |
| Update + rollback | paket rilis palsu yang sengaja crash, cek launcher kembali ke versi lama | VM Windows |
| Screenshot | PC uji dengan user login dan tanpa user login | PC uji unit IT |

`server/scripts/simulate_agents.py` menjadi rujukan cepat untuk urutan panggilan dan bentuk payload.

## Tahapan pengerjaan dan pertanyaan terbuka

Client dikerjakan dalam empat tahap; tiap tahap baru dianggap selesai bila lolos gerbangnya di PC uji. Launcher auto-update selesai sebelum agent disebar ke luar unit IT.

1. **Tahap 1 · Fondasi agent** — config, enroll, heartbeat 30 dtk, sampler metrik + disk, buffer SQLite, batch metrics, retry dengan backoff. Jalan sebagai proses biasa dulu; kontrak API dites terhadap server Ronda asli.
    - Gerbang: 5–10 PC uji tampil akurat, offline terdeteksi kurang dari 3 menit.
    - Status: selesai, teruji di Windows 11 (mode console); gerbang 5–10 PC unit IT belum dijalankan.
2. **Tahap 2 · Launcher dan auto-update** — Windows Service, folder per versi, verifikasi SHA-256 + Ed25519, rollback otomatis, `sign_release.py`. Wajib ada di versi 1.0.0: agent tanpa updater hanya bisa diperbarui manual.
    - Gerbang: update dan rollback lolos di VM Windows dengan paket yang sengaja crash.
    - Status: selesai, gerbang lolos di Windows 11 (`packaging/uji/uji-update.ps1`).
3. **Tahap 3 · Inventaris dan aplikasi** — WMI: spesifikasi, disk, adaptor jaringan; registry Uninstall + hash; perintah `refresh_inventory`. Kolektor Windows dites dengan marker `windows` di runner Windows.
    - Gerbang: daftar aplikasi sama dengan Apps & features di PC uji.
    - Status: selesai, kolektor teruji di Windows 11.
4. **Tahap 4 · Helper screenshot dan installer** — named pipe, mss + JPEG, upload/fail; Inno Setup dengan `/SERVER /KEY /VERYSILENT` untuk GPO. Screenshot aktif bawaan, user diberi tahu bila Admin memilih.
    - Gerbang: screenshot berhasil di PC yang login; tanpa user login tercatat gagal.
    - Status: selesai, build + installer + screenshot teruji di Windows 11; jendela pemberitahuan ke user belum diuji di Windows (`BACA-SAYA-WINDOWS.txt` langkah 5c).
5. **Rilis 1.0.0 bertahap**: Ring 0 unit IT → Ring 1 ±10% PC → Ring 2 semua PC.
    - Status: belum; kunci tanda tangan rilis belum dibuat (`release_keys.py` masih kosong).

Tahap 1 dan 2 boleh berjalan paralel, tetapi tidak ada PC di luar unit IT yang dipasangi sebelum gerbang Tahap 2 lolos.

**Pertanyaan terbuka**

- [x] Apakah tanda tangan Ed25519 dibuat atas byte zip mentah (usulan dokumen ini) atau atas hash SHA-256? Byte zip mentah (`sign_release.py`, `release.verify_package`).
- [x] Siapa yang memegang kunci privat penanda tangan rilis, dan di mesin mana? Dua kunci terenkripsi passphrase, dibuat di mesin build: USB utama dipegang penanggung jawab rilis, USB cadangan di brankas lokasi lain (`agent/README.md` "Kunci tanda tangan rilis").
- [ ] Apakah perlu code signing (Authenticode) untuk `.exe` agar tidak ditandai antivirus?
- [x] Apakah helper wajib menampilkan notifikasi saat screenshot diambil? Ya, sesuai pilihan Admin per permintaan (bawaan ya).
- [ ] Apakah masih ada PC Windows 7/8.1 yang harus didukung? Python 3.13 tidak berjalan di sana.
- [ ] Berapa jumlah PC yang akan dipasangi, untuk menentukan jeda acak unduhan rilis?
