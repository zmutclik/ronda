#!/usr/bin/env bash
# Penjadwal backup di service `backup` (docker-compose.yml): jalankan backup.sh tiap hari pukul BACKUP_TIME (TZ).
# - Saat container start: bila belum ada backup dalam 24 jam terakhir (mis. server mati pada jam backup),
#   backup langsung dibuat.
# - Backup gagal (DB mati, NAS lepas): diulang tiap 30 menit, maksimal 3 kali, lalu menunggu jadwal berikutnya.
#   Healthcheck container menjadi unhealthy bila backup terbaru lebih tua dari 26 jam.
set -uo pipefail

BACKUP_TIME="${BACKUP_TIME:-02:00}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
for v in BACKUP_DB_HOST BACKUP_DB_NAME BACKUP_DB_USER BACKUP_DB_PASSWORD; do
  [[ -n "${!v:-}" ]] || { echo "isi $v di .env" >&2; exit 1; }
done
[[ "$BACKUP_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "BACKUP_TIME harus HH:MM, mis. 02:00" >&2; exit 1; }

# Tidur yang bisa diputus SIGTERM (docker compose stop) tanpa menunggu batas waktu stop
trap 'exit 0' TERM INT
pause() { sleep "$1" & wait $!; }

attempt() {
  for i in 1 2 3; do
    /srv/backup.sh && return 0
    echo "Backup gagal (percobaan $i/3)." >&2
    (( i < 3 )) && pause 1800
  done
  return 1
}

if ! find "$BACKUP_DIR" -maxdepth 1 -name 'ronda-*.sql.gz' -mmin -1440 2>/dev/null | grep -q .; then
  echo "Belum ada backup dalam 24 jam terakhir: backup sekarang."
  attempt
fi

while true; do
  now=$(date +%s)
  next=$(date -d "today $BACKUP_TIME" +%s)
  (( next <= now )) && next=$(date -d "tomorrow $BACKUP_TIME" +%s)
  echo "Backup berikutnya: $(date -d "@$next" '+%Y-%m-%d %H:%M %Z')"
  pause $(( next - now ))
  attempt
done
