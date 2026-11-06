#!/usr/bin/env bash
# Deploy versi baru Ronda di server produksi, setelah `git pull`:
#   1. build image (dependency di pyproject/uv.lock tertanam di image)
#   2. migrasi database (alembic upgrade head) dengan user pemilik skema (ronda_migrate)
#   3. grant tabel baru untuk user aplikasi (deploy/db_admin.py APP_TABLE_GRANTS) dengan user ber-GRANT OPTION (root)
#   4. jalankan container baru dan tunggu sampai sehat
#
#   cd server && git pull && ./deploy/upgrade.sh
#
# Password ditanyakan (tidak tampil, tidak masuk argumen/riwayat shell). Tanpa prompt, isi lewat environment:
#   MIGRATE_USER / MIGRATE_PASSWORD   (default user: ronda_migrate)
#   GRANT_USER   / GRANT_PASSWORD     (default user: root)
# Host & nama database diambil dari DATABASE_URL di .env, jadi tidak perlu ditulis lagi.
# Backup dulu bila perlu: docker compose exec backup /srv/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo "Tidak ada .env di $(pwd)." >&2; exit 1; }

ask() {   # ask VAR "label" default secret
  local var=$1 label=$2 default=${3:-} secret=${4:-}
  [[ -n "${!var:-}" ]] && return
  local value
  if [[ -n $secret ]]; then
    read -rsp "$label: " value; echo
  else
    read -rp "$label [$default]: " value
    value=${value:-$default}
  fi
  printf -v "$var" '%s' "$value"
}

ask MIGRATE_USER "User DB migrasi" ronda_migrate
ask MIGRATE_PASSWORD "Password $MIGRATE_USER" "" secret
ask GRANT_USER "User DB untuk GRANT" root
ask GRANT_PASSWORD "Password $GRANT_USER" "" secret

db_admin() {   # db_admin migrate|grant USER PASSWORD
  DB_ADMIN_USER=$2 DB_ADMIN_PASSWORD=$3 \
    docker compose run --rm -T --no-deps -e DB_ADMIN_USER -e DB_ADMIN_PASSWORD web python - "$1" < deploy/db_admin.py
}

echo "== 1/4 Build image"
docker compose build

echo "== 2/4 Migrasi database"
db_admin migrate "$MIGRATE_USER" "$MIGRATE_PASSWORD"

echo "== 3/4 Grant tabel baru untuk user aplikasi"
db_admin grant "$GRANT_USER" "$GRANT_PASSWORD"

echo "== 4/4 Jalankan container baru"
docker compose up -d --wait

docker compose ps
echo "Selesai. Cek halaman Komputer di browser."
