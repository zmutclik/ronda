# Ronda — Dokumen Pengembangan

Oct 14, 2026 · @Fahrudin Hariadi · diperbarui 16 Okt 2026 sesuai kode

> Dokumen ini rancangan awal. Bagian yang berbeda saat implementasi sudah disesuaikan dengan kode; alasan tiap keputusan ada di `server/README.md` ("Keputusan & perbedaan dari dokumen/skema"). Rincian client ada di `ronda_agent_-_dokumen_pengembangan_client.md`.

## Ringkasan

Ronda adalah aplikasi monitoring komputer client di rumah sakit. Setiap PC dipasangi agent yang melaporkan kondisinya ke server web, sehingga tim IT bisa melihat status semua komputer dari satu dashboard.

**Tujuan**

- Mengetahui PC mana yang online/offline secara real-time.
- Memantau CPU, RAM, disk, dan jaringan setiap PC untuk mendeteksi masalah lebih awal.
- Mencatat inventaris hardware dan aplikasi terpasang.
- Mengambil screenshot layar client saat dibutuhkan untuk troubleshooting.

**Ruang lingkup**

- Client: PC Windows di seluruh unit RS.
- Server: aplikasi web internal, hanya diakses dari jaringan RS.
- Di luar lingkup tahap awal: deployment software dan client Linux/macOS. Remote (noVNC + UltraVNC) ditambahkan
  setelah Tahap 4.

**Pengguna**

| Peran | Kebutuhan |
| --- | --- |
| Admin IT | Akses penuh, termasuk screenshot dan pengaturan |
| Teknisi IT | Melihat status, detail PC, dan alert |
| Manajemen | Melihat dashboard dan laporan inventaris (belum ada role khusus; implementasi hanya Admin dan Teknisi) |

## Kebutuhan

**Fungsional**

| Kode | Kebutuhan | Prioritas | Status |
| --- | --- | --- | --- |
| F-01 | Agent mengirim metrik CPU, RAM, disk, dan jaringan secara berkala | MVP | Selesai |
| F-02 | Server menandai PC offline bila tidak ada heartbeat dalam batas waktu | MVP | Selesai (bawaan 120 dtk) |
| F-03 | Dashboard menampilkan ringkasan status semua PC | MVP | Selesai |
| F-04 | Halaman detail PC dengan spesifikasi dan grafik histori metrik | MVP | Selesai |
| F-05 | Pendaftaran agent dengan token dan persetujuan admin | MVP | Selesai |
| F-06 | Agent mengirim daftar aplikasi terpasang | Tahap 2 | Selesai |
| F-07 | Pengelompokan PC per unit/ruangan | Tahap 2 | Selesai (Folder Unit) |
| F-08 | Screenshot layar client secara on-demand | Tahap 3 | Selesai |
| F-09 | Alert berdasarkan aturan ambang batas, dengan notifikasi Telegram/WA | Tahap 4 | Selesai; belum diuji dengan bot asli |
| F-10 | Rekap inventaris aplikasi di semua PC | Tahap 4 | Belum (daftar aplikasi baru ada per PC) |
| F-11 | Manajemen user, role, dan audit log | Tahap 3 | Selesai |
| F-12 | Export laporan PDF/Excel | Opsional | Selesai (PDF lewat cetak browser, Excel lewat openpyxl) |
| F-13 | Remote layar PC (lihat saja / kontrol penuh) dari browser | Tambahan | Selesai (teruji di Windows 11, UltraVNC 1.8.3) |

**Non-fungsional**

- Agent ringan: target penggunaan CPU di bawah 1% dan RAM di bawah 50 MB saat idle.
- Agent berjalan otomatis saat Windows menyala dan pulih sendiri bila crash.
- Agent tidak membuka port masuk; semua koneksi dimulai dari client ke server.
- Seluruh komunikasi terenkripsi (HTTPS; TLS diakhiri di Nginx Proxy Manager).
- Server mampu menampung seluruh PC RS dengan interval kirim 30–60 detik.
- Data metrik detail disimpan terbatas (bawaan 30 hari), agregat per jam 365 hari.

## Arsitektur sistem

Agent di tiap PC selalu memulai koneksi ke server, sehingga client tidak perlu membuka port dan tetap terjangkau walau IP-nya berubah karena DHCP.

&#91;embedded content: arsitektur Ronda · agent, server, database\]

Metrik dikirim lewat HTTPS secara berkala. Perintah seperti screenshot dititipkan server di respons heartbeat (paling lama satu interval heartbeat, bawaan 30 detik); WebSocket perintah belum dipakai. Agent meneruskan perintah screenshot ke helper di sesi user lewat named pipe, lalu hasilnya diunggah ke server.

Remote: browser (noVNC) dan agent sama-sama membuka WebSocket ke relay di server (`/relay/`, satu proses tersendiri); relay memasangkan keduanya per sesi dan menyalin byte. Agent menyambungkan tunnel itu ke UltraVNC yang hanya mendengarkan di 127.0.0.1 PC dan hanya dinyalakan selama sesi. PC tetap tidak membuka port masuk.

## Tech stack

Semua komponen memakai Python agar satu bahasa dari agent sampai server.

| Komponen | Pilihan | Alasan |
| --- | --- | --- |
| Agent – metrik | psutil | Lintas platform, lengkap untuk CPU, RAM, disk, network |
| Agent – screenshot | mss + Pillow | Cepat, tanpa dependensi berat; Pillow untuk resize + JPEG |
| Agent – app list | winreg (bawaan Python) | Membaca registry Uninstall |
| Agent – komunikasi | httpx | HTTP untuk metrik; perintah lewat respons heartbeat |
| Agent – verifikasi update | cryptography (Ed25519) | Tanda tangan paket rilis |
| Agent – packaging | PyInstaller (onedir) | Jadi .exe, client tidak perlu Python |
| Agent – service | pywin32 | Launcher jalan sebagai Windows Service |
| Agent – installer | Inno Setup | `/VERYSILENT` untuk GPO |
| Backend | FastAPI | Async, sudah familiar |
| ORM & migrasi | SQLAlchemy 2 + Alembic | Standar, migrasi skema terkontrol |
| Database | MariaDB (tabel metrik dipartisi per hari/bulan) | Server RS sudah menjalankan MariaDB |
| Antrean job | Tabel `jobs` di MariaDB | Tanpa Redis |
| Frontend | Jinja2 + HTMX + Alpine.js, grafik SVG dirender server | Ringan, tanpa build step, CSP ketat |
| Reverse proxy | Nginx (di compose) di belakang Nginx Proxy Manager (TLS) | TLS diurus proxy yang sudah ada |
| Deployment | Docker Compose | Konsisten dengan alur DevOps |

Halaman dashboard dirender server (HTML + fragmen HTMX); belum ada API JSON terpisah untuk dashboard.

## Desain agent (Windows)

Agent dipecah jadi tiga proses, karena Windows Service berjalan di Session 0 dan tidak bisa melihat desktop user (screenshot dari service akan hitam atau gagal), dan karena `.exe` yang sedang berjalan tidak bisa ditimpa saat update.

| Proses | Jalan sebagai | Tugas |
| --- | --- | --- |
| `ronda-launcher` | Windows Service `RondaLauncher` (auto-start, restart on failure), LocalSystem | Menjalankan agent dan helper dari folder versi aktif, memasang update, rollback |
| `ronda-agent` | Proses anak launcher, LocalSystem | Metrik, inventaris, app list, heartbeat, perintah, cek update |
| `ronda-helper` | Dijalankan launcher di sesi user konsol (`CreateProcessAsUser`, tanpa Task Scheduler) | Mengambil screenshot saat diminta agent, menampilkan pemberitahuan ke user |

Agent dan helper berkomunikasi lewat named pipe `\\.\pipe\ronda-helper-<sesi>`, dengan autentikasi HMAC dua arah memakai `local.token` yang dibuat launcher.

**Data yang dikumpulkan**

| Data | Sumber | Interval |
| --- | --- | --- |
| CPU % total | `psutil.cpu_percent` | 30 detik |
| RAM % | `psutil.virtual_memory` | 30 detik |
| Disk % per partisi tetap | `psutil.disk_usage` | 5 menit |
| Network in/out (bps) | `psutil.net_io_counters` | 30 detik |
| Latency ke server | TCP connect | 1 menit |
| User login, waktu boot, IP | `psutil.users`, `boot_time`, socket ke server | tiap heartbeat (30 detik) |
| Spesifikasi, disk fisik, adaptor jaringan (MAC, gateway, DNS, Wi-Fi) | `platform`, WMI, `netsh` | Saat online + harian + saat diminta admin |
| Aplikasi terpasang | Registry `HKLM\...\Uninstall`, `WOW6432Node`, `HKU\<SID>` | Bersama inventaris, kirim bila berubah (hash) |

**Perilaku penting**

- Metrik dikirim dalam batch tiap 5 menit; bila server tidak terjangkau, disimpan di buffer SQLite (maks 2 hari / 6.000 baris) lalu dikirim ulang dengan exponential backoff 5 → 300 detik.
- Konfigurasi (URL server, enrollment key, interval, `screenshot`) disimpan di `C:\ProgramData\Ronda\config.toml`; token agent di `state.dat` terenkripsi DPAPI.
- Log berputar (rotating) di `C:\ProgramData\Ronda\logs`; log helper di `%LOCALAPPDATA%\Ronda\logs`.
- Screenshot dikompres ke JPEG (lebar maks 1600 px kualitas 70, lalu 1280 px kualitas 50 bila > 3 MB) sebelum dikirim.
- Installer Inno Setup memasang launcher, agent, helper, dan konfigurasi sekaligus, sehingga bisa disebar lewat GPO.

## Auto-update agent

Agent diperbarui terpusat dari server: admin mengunggah versi baru sekali, lalu semua PC memperbarui dirinya sendiri. Mekanisme ini wajib ada sejak versi pertama yang disebar, karena agent yang belum punya updater hanya bisa diperbarui manual.

**Komponen di client**

| Komponen | Peran |
| --- | --- |
| `ronda-launcher` | Windows Service yang sangat kecil dan jarang berubah. Menjalankan agent dari folder versi aktif, memasang update, dan melakukan rollback. |
| `ronda-agent` | Diinstal per versi di `C:\ProgramData\Ronda\versions\<versi>\`. Bisa diganti tanpa menyentuh launcher. |
| `ronda-helper` | Ikut paket versi yang sama; launcher menjalankan ulang helper di sesi user setelah update atau rollback. |

Pola folder per versi dipakai karena Windows tidak mengizinkan file `.exe` yang sedang berjalan ditimpa.

**Alur update**

1. Admin mengunggah paket agent baru (zip) di menu Pendaftaran Agent → Rilis. Server menyimpan versi, SHA-256, dan tanda tangan digital paket.
2. Agent mengecek `GET /api/agent/update` saat online dan tiap 4 jam (belum ada pemberitahuan lewat WebSocket).
3. Agent menunggu jeda acak 0–30 menit agar ratusan PC tidak mengunduh bersamaan, lalu mengunduh paket.
4. Agent memverifikasi ukuran, SHA-256, dan tanda tangan Ed25519 dengan kunci publik yang tertanam di agent (`release_keys.py`, utama + cadangan). Paket yang gagal verifikasi ditolak.
5. Paket diekstrak ke folder versi baru dan agent keluar dengan kode 75; launcher mengganti pointer versi aktif lalu menjalankan agent dan helper baru.
6. Bila agent baru crash 3 kali dalam 5 menit atau tidak menulis file `healthy` (setelah heartbeat pertama berhasil) dalam 5 menit, launcher otomatis kembali ke versi sebelumnya dan melaporkan `rollback`.
7. Setelah versi baru stabil 24 jam, versi selain current + previous dihapus.

**Rilis bertahap (ring)**

| Ring | Isi | Kapan naik ke ring berikutnya |
| --- | --- | --- |
| Ring 0 | PC unit IT | Stabil 1 hari tanpa rollback |
| Ring 1 | Sekitar 10% PC dari beberapa unit | Stabil 2–3 hari |
| Ring 2 | Semua PC | — |

Dashboard menampilkan sebaran versi agent per PC, status update (menunggu, mengunduh, terpasang, rollback), dan tombol untuk menghentikan rilis bila ada masalah.

**Tambahan di server**

- Tabel `agent_releases` (versi, sha256, signature, status, ring) dan kolom `agent_version`, `update_status` di `hosts`.
- Endpoint `GET /api/agent/update` dan `GET /api/agent/releases/{versi}/download`.
- Kunci privat penanda tangan disimpan di luar server produksi: dua kunci terenkripsi passphrase di USB (utama + cadangan), dipakai di mesin build. Belum dibuat; prosedur di `agent/README.md`.

Launcher sendiri jarang diubah. Paket yang butuh launcher lebih baru (`min_launcher`) ditolak agent; launcher diperbarui lewat installer baru (`/VERYSILENT`, manual atau GPO).

## Desain server

Server terdiri dari API untuk agent, halaman dashboard yang dirender server, dan relay WebSocket untuk remote (proses terpisah, `app/remote/relay.py`). Perintah ke agent dititipkan di respons heartbeat; WebSocket `/ws/agent` belum dibuat.

**API agent** (autentikasi: header `Authorization: Bearer <agent_token>`)

| Method | Endpoint | Fungsi |
| --- | --- | --- |
| POST | `/api/agent/enroll` | Daftar pertama kali dengan enrollment key, mendapat agent token |
| POST | `/api/agent/heartbeat` | Tanda hidup + info singkat |
| POST | `/api/agent/metrics` | Kirim batch metrik |
| PUT | `/api/agent/inventory` | Kirim spesifikasi hardware |
| PUT | `/api/agent/apps` | Kirim daftar aplikasi terpasang |
| POST | `/api/agent/screenshots/{request_id}` | Upload hasil screenshot |
| POST | `/api/agent/screenshots/{request_id}/fail` | Lapor screenshot gagal |
| GET | `/api/agent/update` | Cek rilis yang harus dipasang |
| GET | `/api/agent/releases/{versi}/download` | Unduh paket rilis |

Rincian payload dan kode respons ada di dokumen client.

**Halaman dashboard** (autentikasi: session di database + CSRF; HTML dan fragmen HTMX, bukan API JSON)

| Method | Endpoint | Fungsi |
| --- | --- | --- |
| GET | `/hosts`, `/hosts/rows` | Daftar PC, filter folder/status |
| GET | `/hosts/{id}` | Detail PC |
| GET | `/hosts/{id}/charts` | Grafik histori metrik (SVG) |
| GET | `/hosts/{id}/apps` | Aplikasi terpasang |
| POST | `/hosts/{id}/screenshots` | Minta screenshot (Admin; alasan wajib, tercatat di audit log) |
| GET | `/alerts` | Alert aktif dan riwayat |
| POST | `/agents/approve` | Setujui PC baru |

**Format perintah di respons heartbeat**

```json
{"status": "online", "commands": [{"type": "screenshot", "request_id": "7f3c...", "notify": true}]}
```

Server mengulang perintah di tiap heartbeat selama statusnya masih `requested` (maks 10 menit); agent mengunggah gambar lewat HTTP terpisah.

**Skema database**

Sumber skema adalah migrasi Alembic di `server/migrations/versions/`. Tabel utama:

| Kelompok | Tabel |
| --- | --- |
| PC & inventaris | `hosts`, `host_latest`, `host_disks`, `network_adapters`, `installed_apps`, `host_events`, `folders` |
| Metrik (dipartisi) | `metrics`, `disk_metrics` (harian), `metrics_hourly` (bulanan) |
| Screenshot & remote | `screenshots`, `remote_sessions` |
| Alert | `alert_rules`, `notification_channels`, `alert_rule_channels`, `alerts`, `alert_notes` |
| Agent | `enrollment_keys`, `agent_releases` |
| User & keamanan | `users`, `user_sessions`, `login_failures`, `audit_logs` (dipartisi bulanan) |
| Lain-lain | `settings`, `jobs`, `report_downloads` |

File screenshot disimpan di volume `ronda-data`, bukan di database. Status online dihitung dari `last_seen_at` saat dibaca; worker menandai PC offline dan mencatat Riwayat tiap ±60 detik.

## Struktur menu

| Menu | Isi | Tahap | Status |
| --- | --- | --- | --- |
| Dashboard | Jumlah PC online/offline, PC dengan resource kritis, alert terbaru | MVP | Ada |
| Komputer | Daftar PC: hostname, IP, user login, folder, status, last seen; filter per folder | MVP | Ada |
| Detail Komputer | Spesifikasi, grafik CPU/RAM/disk/network, aplikasi, riwayat, screenshot, remote | MVP (screenshot tahap 3) | Ada |
| Folder Unit | Pengelompokan PC per unit/ruangan | Tahap 2 | Ada |
| Alert | Alert aktif dan riwayat; aturan dan kanal notifikasi (Telegram/WA) di halaman yang sama | Tahap 4 | Ada |
| Inventaris Aplikasi | Rekap aplikasi terpasang di semua PC | Tahap 4 | Belum |
| Pendaftaran Agent | Enrollment key, approval PC baru, rilis & sebaran versi agent | MVP | Ada |
| User & Hak Akses | Akun dan role (Admin, Teknisi) | Tahap 3 | Ada |
| Audit Log | Catatan aktivitas, terutama screenshot | Tahap 3 | Ada |
| Laporan | Export PDF (cetak browser) / Excel | Opsional | Ada |

## Keamanan & privasi

Screenshot adalah fitur paling sensitif, karena layar PC RS bisa menampilkan data pasien. Fitur aktif bawaan dan bisa dimatikan per PC (installer `/SCREENSHOT=0`).

**Autentikasi & komunikasi**

- Semua lalu lintas lewat HTTPS dengan sertifikat dari CA internal RS (`ca_bundle` di agent). Agent selalu memverifikasi sertifikat server; pinning belum dipakai.
- Enrollment memakai enrollment key bersama; PC baru berstatus *pending* sampai disetujui admin.
- Setiap agent mendapat token unik saat enroll. Server hanya menyimpan hash-nya, dan token bisa dicabut per PC.
- Dashboard memakai login dengan password argon2id (15–128 karakter), batas percobaan per user+IP dan per IP, session di database. Integrasi LDAP/AD belum ada.

**Hak akses**

- Role: Admin, Teknisi (matriks hak akses di `server/app/auth/permissions.py`). Role Viewer/Manajemen belum ada.
- Hanya Admin yang boleh meminta screenshot.
- Remote: lihat saja = Admin & Teknisi, kontrol penuh = Admin; hanya pembuka sesi yang bisa membuka layarnya.
- Agent hanya menerima perintah yang terdaftar (screenshot, refresh inventaris, remote). Tidak ada eksekusi perintah bebas.

**Privasi screenshot**

- Hanya on-demand, tidak ada screenshot otomatis berkala.
- Setiap permintaan tercatat di audit log: siapa, kapan, PC mana, dari IP mana.
- Admin wajib mengisi alasan; pemberitahuan kecil muncul di layar user 10 detik setelah gambar diambil, kecuali Admin tidak mencentang "Beri notifikasi ke user".
- File screenshot dihapus otomatis setelah masa simpan (bawaan 7 hari, `settings.retention`).
- Akses ke file screenshot lewat endpoint yang diautentikasi, bukan URL statis publik.

**Remote**

- Aktif bawaan, bisa dimatikan per PC (installer `/REMOTE=0`). Alasan wajib; mulai dan akhir sesi tercatat di Audit Log
  dan Riwayat PC; satu sesi per PC; diputus otomatis setelah 60 menit (`REMOTE_MAX_DURATION`).
- User di PC selalu diberi pemberitahuan saat sesi mulai dan selesai; ikon UltraVNC tampil di tray selama sesi.
- UltraVNC hanya mendengarkan 127.0.0.1, hanya hidup selama sesi, dengan password acak per sesi yang tidak pernah
  keluar dari PC (agent yang login ke VNC). Mode lihat saja ditegakkan di PC (password view-only UltraVNC + input
  dimatikan), bukan hanya di browser.
- WebSocket browser memakai session login + cek Origin (mencegah situs lain membuka sesi dengan cookie user);
  WebSocket agent memakai token agent dan hanya untuk sesi milik PC itu.
- Tanpa transfer file (FileTransferEnabled=0) dan tanpa sinkronisasi clipboard di halaman remote.

**Pertanyaan terbuka**

- [ ] Apakah karyawan perlu diberi pemberitahuan resmi tentang monitoring ini?
- [ ] Berapa lama masa simpan screenshot dan data metrik yang disepakati? Bawaan sementara: metrik 30 hari, agregat per jam 365 hari, screenshot 7 hari, audit log 365 hari.

## Deployment & operasional

Server dijalankan dengan Docker Compose di satu VM internal, di belakang Nginx Proxy Manager yang mengurus TLS. MariaDB memakai server database RS yang sudah ada (di luar compose).

**Struktur repositori**

```
ronda/
├── agent/                # agent, helper, launcher, spec PyInstaller, installer, skrip uji Windows
├── server/
│   ├── app/              # FastAPI per domain: agents, alerts, audit, auth, dashboard, folders, hosts,
│   │                     # integrations, jobs, reports, settings, users (+ core, templates, static)
│   ├── migrations/       # migrasi Alembic
│   ├── deploy/           # nginx.conf.template, backup.sh, backup-loop.sh
│   ├── docker-compose.yml
│   └── Dockerfile
├── .github/workflows/    # CI server + agent
└── *.md                  # dokumen pengembangan (induk + client)
```

**Service di docker-compose**

| Service | Peran |
| --- | --- |
| `nginx` | Reverse proxy HTTP dan file statis, di belakang Nginx Proxy Manager (TLS) |
| `web` | FastAPI (uvicorn, beberapa worker), healthcheck `/healthz` |
| `worker` | Antrean job di tabel `jobs`: cek offline, evaluasi alert, agregat per jam, partisi & retensi |
| `backup` | `mariadb-dump` harian ke NAS (`BACKUP_PATH`), simpan 7 file terakhir |

Tanpa Redis: perintah ke agent lewat respons heartbeat, jadi tidak perlu diteruskan antar worker.

**Operasional**

- Backup database harian (`mariadb-dump`) ke NAS lewat service `backup`.
- Retensi (bisa diubah di `settings.retention`): metrik detail 30 hari, agregat per jam 1 tahun, screenshot 7 hari, audit log 1 tahun; partisi lama dibuang worker.
- Update agent: lewat mekanisme auto-update terpusat dengan rilis bertahap (lihat bagian Auto-update agent). GPO hanya untuk instalasi awal.
- Pantau server Ronda sendiri (disk, container health), misalnya dengan healthcheck Docker dan alert ke Telegram.
- CI (GitHub Actions): lint + test server (dengan MariaDB) dan agent, pip-audit, build image Docker. Installer agent dibangun di mesin build Windows (`agent/packaging/build.ps1`), belum di CI.

## Roadmap

Pengembangan dibagi empat tahap. Tiap tahap selesai bila kriterianya terpenuhi di beberapa PC uji, sebelum disebar ke seluruh RS. Status per 16 Okt 2026: kode keempat tahap sudah ada (kecuali yang disebut di bawah); kriteria selesai di PC uji belum semuanya dijalankan.

1. **Tahap 1 – MVP monitoring**
   - Agent service: CPU, RAM, disk, network, heartbeat, plus launcher auto-update sejak versi pertama.
   - Server: enrollment, penyimpanan metrik, status online/offline.
   - Halaman Dashboard, Komputer, Detail Komputer, Pendaftaran Agent.
   - Selesai bila: 5–10 PC uji tampil akurat di dashboard dan status offline terdeteksi kurang dari 3 menit.
   - Status: kode selesai; uji di 5–10 PC unit IT belum.
2. **Tahap 2 – Inventaris & unit**
   - Spesifikasi hardware dan daftar aplikasi.
   - Pengelompokan PC per unit/ruangan.
   - Selesai bila: daftar aplikasi sama dengan "Apps & features" di PC uji.
   - Status: selesai; kolektor teruji di Windows 11.
3. **Tahap 3 – Screenshot & keamanan**
   - Helper sesi user, perintah lewat respons heartbeat (WebSocket ditunda), upload screenshot.
   - User, role, dan audit log.
   - Selesai bila: screenshot berhasil di PC yang sedang login, dan setiap permintaan tercatat di audit log.
   - Status: selesai, screenshot teruji di Windows 11; jendela pemberitahuan ke user belum diuji di Windows.
4. **Tahap 4 – Alert & laporan**
   - Aturan alert, notifikasi Telegram/WA, rekap inventaris aplikasi.
   - Retensi data dan backup otomatis.
   - Selesai bila: alert disk penuh dan PC offline terkirim ke Telegram.
   - Status: alert, notifikasi, retensi, dan backup sudah ada; rekap inventaris aplikasi (F-10) belum; uji dengan bot Telegram/WA asli belum.

Setelah tahap 1 stabil, agent bisa mulai disebar bertahap per unit lewat GPO, dimulai dari unit IT sendiri.
