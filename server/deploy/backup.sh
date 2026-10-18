#!/usr/bin/env bash
# Backup harian database Ronda: hanya data penting, dengan rotasi.
#
# Isi backup:
# - STRUKTUR semua tabel (restore menghasilkan database lengkap yang langsung bisa dipakai aplikasi);
# - DATA semua tabel, KECUALI yang besar & bisa hilang tanpa merugikan (SKIP_DATA di bawah);
# - metrics_hourly hanya BACKUP_HOURLY_DAYS hari terakhir (bahan laporan bulanan; 0 = tidak ikut).
# File screenshot & paket rilis di volume ronda-data TIDAK ikut (screenshot kedaluwarsa 7 hari; paket rilis
# disimpan di mesin penanda tangan).
#
# Rotasi: setelah backup berhasil, simpan BACKUP_KEEP file terbaru, sisanya dihapus. Dihitung per jumlah file,
# bukan umur: bila backup gagal beberapa hari, backup lama yang masih baik tidak ikut terhapus.
#
# Dijalankan otomatis tiap hari oleh service `backup` di docker-compose.yml (deploy/backup-loop.sh).
# Konfigurasi: variabel BACKUP_* di environment (compose: dari .env), atau file BACKUP_ENV=/path/file.
# Pakai mariadb-dump lokal bila ada; jika tidak, lewat image Docker mariadb (BACKUP_DUMP_IMAGE).
#   docker compose exec backup /srv/backup.sh       backup sekarang, di luar jadwal
set -euo pipefail
umask 077                             # backup berisi hash password & token: hanya pemilik yang bisa membaca

if [[ -n "${BACKUP_ENV:-}" ]]; then
  # shellcheck disable=SC1090
  source "$BACKUP_ENV"
fi

: "${BACKUP_DB_HOST:?isi BACKUP_DB_HOST}" "${BACKUP_DB_NAME:?isi BACKUP_DB_NAME}"
: "${BACKUP_DB_USER:?isi BACKUP_DB_USER}" "${BACKUP_DB_PASSWORD:?isi BACKUP_DB_PASSWORD}"
BACKUP_DB_PORT="${BACKUP_DB_PORT:-3306}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/ronda}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
BACKUP_HOURLY_DAYS="${BACKUP_HOURLY_DAYS:-31}"
BACKUP_DUMP_IMAGE="${BACKUP_DUMP_IMAGE:-mariadb:11.4}"
[[ "$BACKUP_KEEP" =~ ^[1-9][0-9]*$ ]] || { echo "BACKUP_KEEP harus angka ≥ 1" >&2; exit 1; }
[[ "$BACKUP_HOURLY_DAYS" =~ ^[0-9]+$ ]] || { echo "BACKUP_HOURLY_DAYS harus angka ≥ 0" >&2; exit 1; }

# Data tidak di-backup (strukturnya tetap ikut):
#   metrics, disk_metrics  metrik mentah per 30 dtk, tabel terbesar; ringkasannya ada di metrics_hourly
#   metrics_hourly         ditangani terpisah (hanya BACKUP_HOURLY_DAYS hari terakhir)
#   jobs                   antrean kerja sementara (notifikasi, maintenance)
#   user_sessions          session login; setelah restore semua user login ulang
#   login_failures         penghitung lockout login 15 menit
SKIP_DATA=(metrics disk_metrics metrics_hourly jobs user_sessions login_failures)

mkdir -p "$BACKUP_DIR"
exec 9>"$BACKUP_DIR/.lock"
flock -n 9 || { echo "Backup lain masih berjalan." >&2; exit 1; }

# Password lewat option file (bukan argumen: argumen terlihat di `ps`)
cnf="$(mktemp)"
trap 'rm -f "$cnf" "${part:-}"' EXIT
pw="${BACKUP_DB_PASSWORD//\\/\\\\}"
printf '[client]\nhost=%s\nport=%s\nuser=%s\npassword="%s"\n' \
  "$BACKUP_DB_HOST" "$BACKUP_DB_PORT" "$BACKUP_DB_USER" "${pw//\"/\\\"}" > "$cnf"

if command -v mariadb-dump >/dev/null; then
  dump() { mariadb-dump --defaults-extra-file="$cnf" "$@"; }
else
  # --network host: host DB 127.0.0.1 / nama di /etc/hosts sama seperti dari mesin ini
  dump() { docker run --rm --network host -v "$cnf:/backup.cnf:ro" "$BACKUP_DUMP_IMAGE" \
             mariadb-dump --defaults-extra-file=/backup.cnf "$@"; }
fi

# --single-transaction: snapshot konsisten InnoDB tanpa mengunci tabel (aplikasi tetap jalan)
common=(--single-transaction --hex-blob --default-character-set=utf8mb4)
ignore=()
for t in "${SKIP_DATA[@]}"; do ignore+=("--ignore-table=$BACKUP_DB_NAME.$t"); done

name="ronda-$(date +%Y%m%d-%H%M%S).sql.gz"
part="$BACKUP_DIR/.$name.part"
parts=2
{
  dump "${common[@]}" --no-data --routines --events "$BACKUP_DB_NAME"
  dump "${common[@]}" --no-create-info "${ignore[@]}" "$BACKUP_DB_NAME"
  if (( BACKUP_HOURLY_DAYS > 0 )); then
    dump "${common[@]}" --no-create-info \
      --where="hour >= UTC_TIMESTAMP() - INTERVAL $BACKUP_HOURLY_DAYS DAY" "$BACKUP_DB_NAME" metrics_hourly
  fi
} | gzip -6 > "$part"
if (( BACKUP_HOURLY_DAYS > 0 )); then parts=3; fi

# Verifikasi sebelum dianggap sah: gzip utuh dan setiap bagian dump selesai sampai akhir
gzip -t "$part"
done_count="$(gzip -dc "$part" | grep -c '^-- Dump completed' || true)"
[[ "$done_count" == "$parts" ]] || { echo "Dump tidak lengkap ($done_count/$parts bagian)." >&2; exit 1; }
mv "$part" "$BACKUP_DIR/$name"
part=""

# Rotasi: nama berisi tanggal, jadi urutan nama = urutan waktu
mapfile -t old < <(find "$BACKUP_DIR" -maxdepth 1 -name 'ronda-*.sql.gz' -printf '%f\n' | sort -r | tail -n +"$((BACKUP_KEEP + 1))")
for f in "${old[@]}"; do rm -f -- "$BACKUP_DIR/$f"; done
find "$BACKUP_DIR" -maxdepth 1 -name '.ronda-*.part' -mmin +60 -delete   # sisa backup yang terputus

echo "Backup OK: $BACKUP_DIR/$name ($(du -h "$BACKUP_DIR/$name" | cut -f1)); dihapus ${#old[@]} file lama."
