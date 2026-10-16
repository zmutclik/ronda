# Ronda — Dokumen Pengembangan

Oct 14, 2026 · @Fahrudin Hariadi

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
- Di luar lingkup tahap awal: remote control penuh, deployment software, dan client Linux/macOS.

**Pengguna**

| Peran | Kebutuhan |
| --- | --- |
| Admin IT | Akses penuh, termasuk screenshot dan pengaturan |
| Teknisi IT | Melihat status, detail PC, dan alert |
| Manajemen | Melihat dashboard dan laporan inventaris |

## Kebutuhan

**Fungsional**

| Kode | Kebutuhan | Prioritas |
| --- | --- | --- |
| F-01 | Agent mengirim metrik CPU, RAM, disk, dan jaringan secara berkala | MVP |
| F-02 | Server menandai PC offline bila tidak ada heartbeat dalam batas waktu | MVP |
| F-03 | Dashboard menampilkan ringkasan status semua PC | MVP |
| F-04 | Halaman detail PC dengan spesifikasi dan grafik histori metrik | MVP |
| F-05 | Pendaftaran agent dengan token dan persetujuan admin | MVP |
| F-06 | Agent mengirim daftar aplikasi terpasang | Tahap 2 |
| F-07 | Pengelompokan PC per unit/ruangan | Tahap 2 |
| F-08 | Screenshot layar client secara on-demand | Tahap 3 |
| F-09 | Alert berdasarkan aturan ambang batas, dengan notifikasi Telegram/WA | Tahap 4 |
| F-10 | Rekap inventaris aplikasi di semua PC | Tahap 4 |
| F-11 | Manajemen user, role, dan audit log | Tahap 3 |
| F-12 | Export laporan PDF/Excel | Opsional |

**Non-fungsional**

- Agent ringan: target penggunaan CPU di bawah 1% dan RAM di bawah 50 MB saat idle.
- Agent berjalan otomatis saat Windows menyala dan pulih sendiri bila crash.
- Agent tidak membuka port masuk; semua koneksi dimulai dari client ke server.
- Seluruh komunikasi terenkripsi (HTTPS/WSS).
- Server mampu menampung seluruh PC RS dengan interval kirim 30–60 detik.
- Data metrik detail disimpan terbatas (misalnya 30 hari), data ringkasan lebih lama.

## Arsitektur sistem

Agent di tiap PC selalu memulai koneksi ke server, sehingga client tidak perlu membuka port dan tetap terjangkau walau IP-nya berubah karena DHCP.

&#91;embedded content: arsitektur Ronda · agent, server, database\]

Metrik dikirim lewat HTTPS secara berkala, sedangkan perintah seperti screenshot turun lewat WebSocket yang selalu terbuka. Service meneruskan perintah screenshot ke helper di sesi user, lalu hasilnya diunggah ke server.

## Tech stack

Semua komponen memakai Python agar satu bahasa dari agent sampai server.

| Komponen | Pilihan | Alasan |
| --- | --- | --- |
| Agent – metrik | psutil | Lintas platform, lengkap untuk CPU, RAM, disk, network |
| Agent – screenshot | mss | Cepat, tanpa dependensi berat |
| Agent – app list | winreg (bawaan Python) | Membaca registry Uninstall |
| Agent – komunikasi | httpx + websockets | HTTP untuk metrik, WebSocket untuk perintah |
| Agent – packaging | PyInstaller atau Nuitka | Jadi .exe, client tidak perlu Python |
| Agent – service | NSSM atau pywin32 | Jalan sebagai Windows Service |
| Backend | FastAPI | Async, native WebSocket, sudah familiar |
| ORM & migrasi | SQLAlchemy 2 + Alembic | Standar, migrasi skema terkontrol |
| Database | PostgreSQL (+ TimescaleDB opsional) | Relasional + time-series untuk metrik |
| Cache/pub-sub | Redis (opsional) | Status online & meneruskan perintah antar worker |
| Frontend | Jinja2 + HTMX + Chart.js | Ringan, tanpa build step |
| Reverse proxy | Nginx | TLS dan proxy WebSocket |
| Deployment | Docker Compose | Konsisten dengan alur DevOps |

Frontend bisa diganti Vue/React di kemudian hari tanpa mengubah API, karena backend menyediakan endpoint JSON terpisah dari halaman HTML.

## Desain agent (Windows)

Agent dipecah jadi dua proses, karena Windows Service berjalan di Session 0 dan tidak bisa melihat desktop user. Screenshot dari service akan hitam atau gagal.

| Proses | Jalan sebagai | Tugas |
| --- | --- | --- |
| `ronda-agent` | Windows Service (auto-start, restart on failure) | Metrik, app list, heartbeat, koneksi WebSocket ke server |
| `ronda-helper` | Task Scheduler "at logon", di sesi user | Mengambil screenshot saat diminta service |

Service dan helper berkomunikasi lewat named pipe atau HTTP di `127.0.0.1`, dilindungi token lokal yang dibuat saat instalasi.

**Data yang dikumpulkan**

| Data | Sumber | Interval |
| --- | --- | --- |
| CPU % per core & total | `psutil.cpu_percent` | 30–60 detik |
| RAM terpakai/total | `psutil.virtual_memory` | 30–60 detik |
| Disk per partisi | `psutil.disk_usage` | 5 menit |
| Network bytes in/out, IP, MAC | `psutil.net_io_counters`, `net_if_addrs` | 30–60 detik |
| Latency ke gateway/server | ping atau TCP connect | 1 menit |
| User login, uptime | `psutil.users`, `boot_time` | 1 menit |
| Spesifikasi (OS, CPU, RAM, serial) | `platform`, WMI | Saat start + harian |
| Aplikasi terpasang | Registry `HKLM\...\Uninstall`, `WOW6432Node`, `HKCU` | Harian, kirim bila berubah (hash) |

**Perilaku penting**

- Metrik dikirim dalam batch; bila server tidak terjangkau, disimpan sementara di buffer lokal (misalnya SQLite) lalu dikirim ulang.
- WebSocket reconnect otomatis dengan exponential backoff.
- Konfigurasi (URL server, token, interval) disimpan di `C:\ProgramData\Ronda\config.toml`.
- Log berputar (rotating) di `C:\ProgramData\Ronda\logs`.
- Screenshot dikompres ke JPEG/WebP dan diperkecil (misalnya lebar maks 1600 px) sebelum dikirim.
- Installer (Inno Setup atau MSI) memasang service, helper, dan konfigurasi sekaligus, sehingga bisa disebar lewat GPO.

## Auto-update agent

Agent diperbarui terpusat dari server: admin mengunggah versi baru sekali, lalu semua PC memperbarui dirinya sendiri. Mekanisme ini wajib ada sejak versi pertama yang disebar, karena agent yang belum punya updater hanya bisa diperbarui manual.

**Komponen di client**

| Komponen | Peran |
| --- | --- |
| `ronda-launcher` | Windows Service yang sangat kecil dan jarang berubah. Menjalankan agent dari folder versi aktif, memasang update, dan melakukan rollback. |
| `ronda-agent` | Diinstal per versi di `C:\ProgramData\Ronda\versions\<versi>\`. Bisa diganti tanpa menyentuh launcher. |
| `ronda-helper` | Ikut paket versi yang sama; launcher menjalankan ulang helper di sesi user setelah update. |

Pola folder per versi dipakai karena Windows tidak mengizinkan file `.exe` yang sedang berjalan ditimpa.

**Alur update**

1. Admin mengunggah paket agent baru (zip) di menu Pendaftaran Agent → Rilis. Server menyimpan versi, SHA-256, dan tanda tangan digital paket.
2. Server memberi tahu agent lewat WebSocket (`{"type": "update_available", "version": "1.3.0"}`), atau agent mengecek `GET /api/agent/update` tiap beberapa jam sebagai cadangan.
3. Agent menunggu jeda acak (misalnya 0–30 menit) agar ratusan PC tidak mengunduh bersamaan, lalu mengunduh paket.
4. Launcher memverifikasi SHA-256 dan tanda tangan (Ed25519, kunci publik tertanam di launcher). Paket yang gagal verifikasi ditolak.
5. Paket diekstrak ke folder versi baru, pointer versi aktif diganti, agent lama dihentikan dan agent baru dijalankan.
6. Bila agent baru tidak mengirim heartbeat dalam 5 menit atau crash berulang, launcher otomatis kembali ke versi sebelumnya dan melaporkan kegagalan.
7. Versi lama dihapus setelah versi baru stabil; cukup simpan 2 versi terakhir.

**Rilis bertahap (ring)**

| Ring | Isi | Kapan naik ke ring berikutnya |
| --- | --- | --- |
| Ring 0 | PC unit IT | Stabil 1 hari tanpa rollback |
| Ring 1 | Sekitar 10% PC dari beberapa unit | Stabil 2–3 hari |
| Ring 2 | Semua PC | — |

Dashboard menampilkan sebaran versi agent per PC, status update (menunggu, mengunduh, terpasang, rollback), dan tombol untuk menghentikan rilis bila ada masalah.

**Tambahan di server**

- Tabel `agent_releases` (versi, sha256, signature, ring, created\_at) dan kolom `agent_version`, `update_status` di `hosts`.
- Endpoint `GET /api/agent/update` dan `GET /api/agent/releases/{versi}/download`.
- Kunci privat penanda tangan disimpan di luar server produksi (misalnya di mesin build/CI), bukan di container.

Launcher sendiri jarang diubah. Bila suatu saat perlu diperbarui, launcher bisa memanggil installer baru secara diam-diam (`/VERYSILENT`) sebagai satu-satunya pengecualian.

## Desain server

Server terdiri dari API untuk agent, API untuk dashboard, dan satu kanal WebSocket per agent untuk perintah.

**API agent** (autentikasi: header `Authorization: Bearer <agent_token>`)

| Method | Endpoint | Fungsi |
| --- | --- | --- |
| POST | `/api/agent/enroll` | Daftar pertama kali dengan enrollment key, mendapat agent token |
| POST | `/api/agent/heartbeat` | Tanda hidup + info singkat |
| POST | `/api/agent/metrics` | Kirim batch metrik |
| PUT | `/api/agent/inventory` | Kirim spesifikasi hardware |
| PUT | `/api/agent/apps` | Kirim daftar aplikasi terpasang |
| POST | `/api/agent/screenshots/{request_id}` | Upload hasil screenshot |
| WS | `/ws/agent` | Kanal perintah dari server ke agent |

**API dashboard** (autentikasi: session/JWT user admin)

| Method | Endpoint | Fungsi |
| --- | --- | --- |
| GET | `/api/hosts` | Daftar PC, filter unit/status |
| GET | `/api/hosts/{id}` | Detail PC |
| GET | `/api/hosts/{id}/metrics?from=&to=` | Histori metrik untuk grafik |
| GET | `/api/hosts/{id}/apps` | Aplikasi terpasang |
| POST | `/api/hosts/{id}/screenshot` | Minta screenshot (tercatat di audit log) |
| GET | `/api/alerts` | Alert aktif dan riwayat |
| POST | `/api/enrollment/approve/{id}` | Setujui PC baru |

**Format pesan WebSocket**

```json
{"type": "screenshot", "request_id": "7f3c...", "issued_at": "2026-10-14T11:40:00+07:00"}
```

Agent membalas `{"type": "ack", "request_id": "..."}`, lalu mengunggah gambar lewat HTTP. Gambar tidak dikirim lewat WebSocket agar kanal perintah tetap ringan.

**Skema database (inti)**

| Tabel | Kolom utama |
| --- | --- |
| `hosts` | id, hostname, agent\_token\_hash, unit\_id, ip, mac, os, status, approved, last\_seen |
| `units` | id, nama, lokasi |
| `host_specs` | host\_id, cpu\_model, ram\_total, disks (JSON), serial, updated\_at |
| `metrics` | host\_id, ts, cpu\_pct, ram\_pct, net\_in, net\_out, latency\_ms (hypertable bila pakai TimescaleDB) |
| `disk_metrics` | host\_id, ts, mount, used, total |
| `installed_apps` | host\_id, name, version, publisher, install\_date |
| `screenshots` | id, host\_id, requested\_by, requested\_at, file\_path, status |
| `alerts` | id, host\_id, rule\_id, level, message, started\_at, resolved\_at |
| `alert_rules` | id, metric, operator, threshold, duration, channel |
| `users` | id, username, password\_hash, role |
| `audit_logs` | id, user\_id, action, target, ts, ip |

File screenshot disimpan di volume terpisah (atau object storage seperti MinIO), bukan di database. Status online dihitung dari `last_seen`; job berkala menandai PC offline bila melewati batas waktu.

## Struktur menu

| Menu | Isi | Tahap |
| --- | --- | --- |
| Dashboard | Jumlah PC online/offline, PC dengan resource kritis, alert terbaru | MVP |
| Komputer | Daftar PC: hostname, IP, user login, unit, status, last seen; filter per unit | MVP |
| Detail Komputer | Spesifikasi, grafik CPU/RAM/disk/network, aplikasi, screenshot & riwayatnya | MVP (screenshot tahap 3) |
| Unit / Ruangan | Pengelompokan PC per lokasi | Tahap 2 |
| Alert | Alert aktif dan riwayat | Tahap 4 |
| Inventaris Aplikasi | Rekap aplikasi terpasang di semua PC | Tahap 4 |
| Aturan Alert | Ambang batas dan kanal notifikasi (Telegram/WA) | Tahap 4 |
| Pendaftaran Agent | Enrollment key, unduh installer, approval PC baru, rilis & sebaran versi agent | MVP |
| User & Hak Akses | Akun admin dan role | Tahap 3 |
| Audit Log | Catatan aktivitas, terutama screenshot | Tahap 3 |
| Laporan | Export PDF/Excel | Opsional |

## Keamanan & privasi

Screenshot adalah fitur paling sensitif, karena layar PC RS bisa menampilkan data pasien. Fitur ini perlu dasar kebijakan dari manajemen sebelum diaktifkan.

**Autentikasi & komunikasi**

- Semua lalu lintas lewat HTTPS/WSS dengan sertifikat dari CA internal RS. Agent memverifikasi sertifikat server (bila perlu dengan pinning).
- Enrollment memakai enrollment key bersama; PC baru berstatus *pending* sampai disetujui admin.
- Setiap agent mendapat token unik. Server hanya menyimpan hash-nya, dan token bisa dicabut per PC.
- Dashboard memakai login dengan password ter-hash (argon2/bcrypt). Bisa diintegrasikan ke LDAP/AD bila tersedia.

**Hak akses**

- Role: Admin, Teknisi, Viewer.
- Hanya Admin yang boleh meminta screenshot.
- Agent hanya menerima perintah yang terdaftar (screenshot, refresh inventaris). Tidak ada eksekusi perintah bebas.

**Privasi screenshot**

- Hanya on-demand, tidak ada screenshot otomatis berkala.
- Setiap permintaan tercatat di audit log: siapa, kapan, PC mana, dari IP mana.
- Opsional: notifikasi kecil di layar user saat screenshot diambil.
- File screenshot dihapus otomatis setelah masa simpan tertentu (misalnya 7 hari).
- Akses ke file screenshot lewat endpoint yang diautentikasi, bukan URL statis publik.

**Pertanyaan terbuka**

- [ ] Apakah perlu persetujuan tertulis dari direksi/manajemen untuk fitur screenshot?
- [ ] Apakah karyawan perlu diberi pemberitahuan resmi tentang monitoring ini?
- [ ] Berapa lama masa simpan screenshot dan data metrik yang disepakati?

## Deployment & operasional

Server dijalankan dengan Docker Compose di satu VM internal.

**Struktur repositori**

```
ronda/
├── agent/            # kode agent + helper, spec PyInstaller, installer
├── server/
│   ├── app/          # FastAPI: api/, ws/, models/, services/, templates/
│   ├── alembic/      # migrasi database
│   └── Dockerfile
├── deploy/
│   ├── docker-compose.yml
│   └── nginx/
└── docs/
```

**Service di docker-compose**

| Service | Peran |
| --- | --- |
| `nginx` | TLS, reverse proxy HTTP & WebSocket |
| `api` | FastAPI (uvicorn, beberapa worker) |
| `worker` | Job berkala: cek offline, evaluasi alert, hapus data lama |
| `db` | PostgreSQL (+ TimescaleDB) |
| `redis` | Status online & pub/sub perintah antar worker (opsional) |

Bila API memakai lebih dari satu worker, perintah screenshot perlu diteruskan lewat Redis pub/sub, karena koneksi WebSocket agent hanya ada di salah satu worker.

**Operasional**

- Backup database harian (pg\_dump) ke NAS.
- Retensi: metrik detail 30 hari, agregat per jam 1 tahun, screenshot 7 hari.
- Update agent: lewat mekanisme auto-update terpusat dengan rilis bertahap (lihat bagian Auto-update agent). GPO hanya untuk instalasi awal.
- Pantau server Ronda sendiri (disk, container health), misalnya dengan healthcheck Docker dan alert ke Telegram.
- CI: lint + test untuk server dan agent, build image Docker, build installer agent.

## Roadmap

Pengembangan dibagi empat tahap. Tiap tahap selesai bila kriterianya terpenuhi di beberapa PC uji, sebelum disebar ke seluruh RS.

1. **Tahap 1 – MVP monitoring**
   - Agent service: CPU, RAM, disk, network, heartbeat, plus launcher auto-update sejak versi pertama.
   - Server: enrollment, penyimpanan metrik, status online/offline.
   - Halaman Dashboard, Komputer, Detail Komputer, Pendaftaran Agent.
   - Selesai bila: 5–10 PC uji tampil akurat di dashboard dan status offline terdeteksi kurang dari 3 menit.
2. **Tahap 2 – Inventaris & unit**
   - Spesifikasi hardware dan daftar aplikasi.
   - Pengelompokan PC per unit/ruangan.
   - Selesai bila: daftar aplikasi sama dengan "Apps & features" di PC uji.
3. **Tahap 3 – Screenshot & keamanan**
   - Helper sesi user, kanal WebSocket, upload screenshot.
   - User, role, dan audit log.
   - Selesai bila: screenshot berhasil di PC yang sedang login, dan setiap permintaan tercatat di audit log.
4. **Tahap 4 – Alert & laporan**
   - Aturan alert, notifikasi Telegram/WA, rekap inventaris aplikasi.
   - Retensi data dan backup otomatis.
   - Selesai bila: alert disk penuh dan PC offline terkirim ke Telegram.

Setelah tahap 1 stabil, agent bisa mulai disebar bertahap per unit lewat GPO, dimulai dari unit IT sendiri.
