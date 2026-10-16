# Ronda — server

Aplikasi web monitoring komputer client rumah sakit: API untuk agent, dashboard untuk tim IT, dan worker
periodik. Stack mengikuti skill `fullstack-architecture`: FastAPI + Jinja2 + htmx 2 + Alpine (build CSP) +
MariaDB (SQLAlchemy async/asyncmy, Alembic), tanpa Redis. Sumber skema: migrasi Alembic; perbedaan dari desain
awal ada di bagian **Keputusan** di bawah.

## Peta domain

| Domain | Isi | Menu |
|---|---|---|
| `app/auth` | user, session, CSRF, rate limit login, hak akses per role (`permissions.py`), penulis Audit Log | Login, Ganti password |
| `app/folders` | folder Unit › Ruangan (2 tingkat), saran folder dari subnet | Folder Unit |
| `app/hosts` | PC, kondisi terakhir, metrik berpartisi, disk, NIC, aplikasi, riwayat, screenshot; ingest data agent; job offline/agregat/partisi | Komputer, Detail Komputer |
| `app/agents` | API agent `/api/agent/*`, enrollment key, persetujuan PC, rilis bertahap | Pendaftaran Agent |
| `app/alerts` | aturan, evaluator (worker), aksi akui/bisukan/selesaikan, kanal & notifikasi Telegram/WA | Alert |
| `app/dashboard`, `app/users`, `app/audit`, `app/reports` | halaman gabungan / admin | Dashboard, User & Hak Akses, Audit Log, Laporan |
| `app/jobs` | addon antrean + worker (maintenance tiap ±60 detik) | — |
| `app/integrations` | adapter Telegram & WhatsApp (go-whatsapp-web-multidevice) | — |

Dependensi satu arah: `dashboard`, `reports` → `alerts`, `agents` → `hosts` → `folders`, `settings` → `auth` → `core`.

## Development

```bash
uv sync
./scripts/fetch_vendor.sh                 # htmx + Alpine CSP (sudah ter-commit di app/static/js/vendor)
cp .env.example .env                      # DATABASE_URL (utf8mb4), SECRET_KEY, DEBUG=true, COOKIE_SECURE=false
uv run alembic upgrade head
uv run python -m app.auth.cli create-user admin.nama --role admin --name "Nama Lengkap"

uv run uvicorn app.main:app --reload      # web
uv run python -m app.jobs.worker          # worker: offline, alert, agregat, partisi, notifikasi

# Data contoh: 40 PC palsu lewat API agent asli (folder, key, backfill metrik 24 jam, heartbeat berkala)
uv run python -m scripts.simulate_agents --count 40 --backfill-hours 24 --loop 3600
```

## Test

Butuh MariaDB asli; nama database wajib berakhiran `_test` (dikosongkan oleh test suite).

```bash
docker run -d --name ronda-test-mariadb -p 127.0.0.1:33061:3306 \
  -e MARIADB_ROOT_PASSWORD=root -e MARIADB_DATABASE=ronda_test \
  -e MARIADB_USER=ronda -e MARIADB_PASSWORD=ronda mariadb:11.4 \
  --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci
TEST_DATABASE_URL="mysql+asyncmy://ronda:ronda@127.0.0.1:33061/ronda_test" uv run pytest
uv run ruff check app tests scripts
```

Status 2026-10-16 (MariaDB 11.4): 92 test lulus, ruff bersih. Cakupan tambahan di luar
template: API agent (enroll/pending/heartbeat/metrik batch/inventaris/aplikasi/screenshot), evaluator alert
(buka sekali, selesai otomatis, jendela durasi, offline), notifikasi + jam tenang, maintenance (offline, agregat
per jam, partisi & retensi), hak akses per role, Audit Log, laporan, dan larangan `style=`/`<script>` inline
(CSP). Uji mutasi: sisipan `style=` di template dan query sebelum `begin()` di router sama-sama tertangkap.

## Deploy (Docker Compose)

```bash
docker compose build
docker compose run --rm web alembic upgrade head          # sekali per deploy, sebelum up
docker compose run --rm web python -m app.auth.cli create-user admin.nama --role admin --name "..."
docker compose up -d                                       # web + worker + nginx
```

- `.env`: `DATABASE_URL` (user `ronda_app`, tanpa UPDATE/DELETE di `audit_logs`) dan `WORKER_DATABASE_URL`
  (user `ronda_worker`, hak ALTER/DROP untuk partisi) — lihat "Hak akses database" di bawah.
- Volume `ronda-data`: screenshot & paket rilis. File screenshot hanya bisa dibuka lewat endpoint ber-auth (Admin)
  dan setiap pembukaan tercatat di Audit Log.
- TLS diterminasi reverse proxy di depan nginx Ronda (Nginx Proxy Manager), lihat "Di belakang Nginx Proxy Manager".
- Koneksi DB: `WEB_CONCURRENCY × (DB_POOL_SIZE + DB_MAX_OVERFLOW) + (WORKER_CONCURRENCY + 2)` + cadangan.

### Di belakang Nginx Proxy Manager

nginx Ronda (`deploy/nginx.conf.template`) hanya HTTP; TLS dipasang di Nginx Proxy Manager (NPM).

1. `.env`: `TRUSTED_PROXY` = IP NPM sebagaimana terlihat oleh nginx Ronda (atau subnet CIDR). NPM container di host
   yang sama: biasanya gateway network compose (`172.30.0.1`) atau IP host; pastikan lewat
   `docker compose logs nginx` (alamat client di access log saat membuka Ronda lewat NPM).
2. NPM → Proxy Host: scheme `http`, forward ke IP host Ronda port `HTTP_PORT`; tab SSL: sertifikat, Force SSL,
   HSTS Enabled.
3. Agent dan browser memakai `https://<domain NPM>`.

Hanya request dari `TRUSTED_PROXY` yang boleh menentukan IP asli user (`X-Forwarded-For`, dipakai rate limit &
lockout login per IP) dan skema `https` (`X-Forwarded-Proto`, dipakai `server_url` di halaman Pendaftaran Agent).
Request langsung ke port Ronda dari IP lain tetap dilayani, tetapi header itu diabaikan. Bila NPM di host yang
sama, port Ronda bisa ditutup dari luar dengan firewall.

### Hak akses database

Aplikasi memakai user tanpa hak `UPDATE`/`DELETE` pada `audit_logs`, sehingga log benar-benar tidak bisa diubah:

```sql
CREATE USER 'ronda_app'@'%' IDENTIFIED BY '...';
-- grant per tabel (bukan ronda.*), contoh:
GRANT SELECT, INSERT, UPDATE, DELETE ON ronda.hosts  TO 'ronda_app'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON ronda.alerts TO 'ronda_app'@'%';
-- ... tabel lain ...
GRANT SELECT, INSERT ON ronda.audit_logs TO 'ronda_app'@'%';     -- tanpa UPDATE/DELETE
CREATE USER 'ronda_worker'@'%' IDENTIFIED BY '...';               -- untuk ALTER PARTITION
GRANT SELECT, INSERT, UPDATE, DELETE, ALTER, DROP ON ronda.* TO 'ronda_worker'@'%';
```

Hak satu tabel tidak bisa dicabut dari grant tingkat database (`ronda.*`), jadi grant `ronda_app` harus ditulis
per tabel.

### Partisi & retensi

| Tabel | Retensi | Partisi |
|---|---|---|
| `metrics`, `disk_metrics` | 30 hari | per hari |
| `metrics_hourly`, `audit_logs` | 1 tahun | per bulan |

Tabel berpartisi tidak memakai FOREIGN KEY, jadi integritas `host_id`/`user_id` dijaga aplikasi. Retensi dikerjakan
dengan `DROP PARTITION` oleh worker (`app/hosts/tasks.py`).

### Backup database

`deploy/backup.sh` membuat satu file `ronda-YYYYmmdd-HHMMSS.sql.gz` per hari, isinya hanya yang penting:

| Ikut | Tidak ikut (struktur tabel tetap ikut, isinya kosong setelah restore) |
|---|---|
| Struktur semua tabel; data PC, inventaris, aplikasi, folder, user, aturan & riwayat alert, audit log, rilis, settings; `metrics_hourly` 31 hari terakhir | `metrics`, `disk_metrics` (metrik mentah 30 dtk), `jobs`, `user_sessions`, `login_failures`; file screenshot & paket rilis di volume |

Rotasi: setelah backup lolos verifikasi (gzip utuh, setiap bagian dump selesai), hanya `BACKUP_KEEP` (7) file
terbaru yang disimpan. Backup yang gagal tidak menghapus apa pun.

Jadwal otomatis lewat service `backup` di `docker-compose.yml` (ikut `docker compose up -d`; `up` menolak jalan
bila `BACKUP_DB_*` di `.env` belum diisi):

- Tiap hari pukul `BACKUP_TIME` (bawaan 02:00 WIB). Saat container start dan belum ada backup dalam 24 jam
  terakhir (mis. server mati pada jam backup), backup langsung dibuat.
- Gagal (DB mati, NAS lepas): diulang tiap 30 menit, maksimal 3 kali. Backup terbaru lebih tua dari 26 jam
  → container `backup` berstatus **unhealthy** di `docker compose ps`.
- Lokasi: `BACKUP_PATH` di `.env` (bawaan `./backups`), di-mount ke `/backups` dalam container. Untuk NAS, mount
  NAS di host (NFS/CIFS lewat `/etc/fstab`, mis. ke `/mnt/nas/ronda`) lalu isi `BACKUP_PATH=/mnt/nas/ronda`.
  NFS dengan `root_squash`: isi `BACKUP_UID_GID` dengan pemilik folder di NAS.

```sql
CREATE USER 'ronda_backup'@'%' IDENTIFIED BY '...';           -- hanya baca
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT ON ronda.* TO 'ronda_backup'@'%';
```

```bash
docker compose up -d backup                       # setelah BACKUP_* di .env diisi
docker compose logs backup                        # hasil backup & jadwal berikutnya
docker compose exec backup /srv/backup.sh         # backup sekarang, di luar jadwal
```

Restore (ke database kosong utf8mb4; hentikan `web` & `worker` dulu):

```bash
gzip -dc /mnt/nas/ronda/ronda-20261017-020000.sql.gz | mariadb -h HOST -u root -p ronda
docker compose up -d            # worker membuat ulang partisi yang belum ada; semua user login ulang
```

## Keputusan & perbedaan dari dokumen/skema

| Topik | Keputusan | Alasan |
|---|---|---|
| Database | **MariaDB** (bukan MySQL 8.4 di desain skema awal, bukan PostgreSQL di dokumen pengembangan) | Stack skill; server RS sudah menjalankan MariaDB. Semua fitur skema (CHECK, kolom generated, partisi) didukung |
| Sumber skema | Migrasi Alembic `migrations/versions/*initial_schema.py` | Desain awal (MySQL) sudah diporting ke migrasi ini; perubahan berikutnya lewat migrasi |
| Zona waktu | Kolom DATETIME = **UTC**, tampil WIB (filter `localtime`) | Aturan skill: `NOW()` sama di semua instance; partisi harian dihitung per hari UTC |
| Password | argon2id, 15–128 karakter (NIST SP 800-63B-4); password sementara wajib diganti sebelum halaman lain bisa dibuka (dicek tiap request) | Redirect sesudah login saja bisa dilewati dengan membuka URL langsung |
| Lockout login | Tabel `login_failures` per (user, IP) + per IP; kolom `users.failed_logins/locked_until` tidak dipakai | Kunci per user saja memungkinkan siapa pun mengunci akun orang lain |
| Session | Tabel `user_sessions` (tambahan) | Session di DB, stateless antar-instance |
| Kolom tambahan | `hosts.problem_level/text/count`, `hosts.inventory_requested_at`, `hosts.cpu_threads`, `host_latest.disk_max_mount/logged_since`, `host_events.level`, `metrics_hourly.net_in_avg/net_out_avg`, `alerts.resolve_note` | Ringkasan "Kondisi" tanpa join ke alerts (domain satu arah), perintah refresh inventaris, teks UI prototype |
| Token agent | Diterbitkan saat **enroll** (status `pending`), bukan saat disetujui | Agent langsung punya identitas; selama pending hanya heartbeat yang diterima |
| Perintah ke agent | Lewat **respons heartbeat** (`commands`), belum WebSocket | Tanpa Redis pub/sub antar worker uvicorn; latensi screenshot ≤ interval heartbeat (30–60 dtk) |
| Status online | Dihitung dari `last_seen_at` saat dibaca; worker menulis `status` + Riwayat tiap ±60 dtk | Tampilan tidak menunggu job; `offline_after_sec` bawaan 120 dtk (target < 3 menit) |
| Grafik | SVG dirender server (tanpa Chart.js) | CSP ketat tanpa library tambahan |
| Laporan PDF | Halaman cetak → "Simpan sebagai PDF" di browser; Excel lewat openpyxl | Tanpa dependensi render PDF di server |
| Remote (noVNC) | Tab ada (riwayat + kesiapan), fitur **belum aktif** | Di luar lingkup tahap awal menurut dokumen pengembangan |
| Role | 2 role (Admin, Teknisi) sesuai prototype; dokumen menyebut juga Manajemen/Viewer | Matriks hak akses di `app/auth/permissions.py` |

## Belum dikerjakan

- Agent Windows (`../agent/`): service, helper screenshot, launcher auto-update. Kontrak API-nya ada di
  `app/hosts/schemas.py` dan `app/agents/router.py`; `scripts/simulate_agents.py` contoh pemakaiannya.
- WebSocket `/ws/agent`, remote noVNC, rekap inventaris aplikasi lintas PC (F-10).
- Kebijakan screenshot (persetujuan direksi, pemberitahuan karyawan, masa simpan) — pertanyaan terbuka di dokumen.
