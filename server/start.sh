#!/usr/bin/env bash
# Jalankan Ronda untuk development: MariaDB dev (Docker), migrasi, worker, dan web (auto-reload).
# Port 8034; server lama di port ini dimatikan dulu.
#   ./start.sh            web + worker
#   ./start.sh --no-worker
set -euo pipefail
cd "$(dirname "$0")"

PORT=8034
DB_CONTAINER=ronda-test-mariadb
WITH_WORKER=1
[[ "${1:-}" == "--no-worker" ]] && WITH_WORKER=0

[[ -f .env ]] || { echo "Belum ada .env — salin dari .env.example lalu isi DATABASE_URL & SECRET_KEY."; exit 1; }

# MariaDB dev (port 33061, database ronda_dev); dibuat bila belum ada
if ! docker ps --format '{{.Names}}' | grep -qx "$DB_CONTAINER"; then
  if docker ps -a --format '{{.Names}}' | grep -qx "$DB_CONTAINER"; then
    docker start "$DB_CONTAINER" >/dev/null
  else
    docker run -d --name "$DB_CONTAINER" -p 127.0.0.1:33061:3306 \
      -e MARIADB_ROOT_PASSWORD=root -e MARIADB_DATABASE=ronda_test \
      -e MARIADB_USER=ronda -e MARIADB_PASSWORD=ronda mariadb:11.4 \
      --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci >/dev/null
  fi
  echo -n "Menunggu MariaDB"
  until docker exec "$DB_CONTAINER" mariadb -uroot -proot -e "SELECT 1" >/dev/null 2>&1; do echo -n "."; sleep 1; done
  echo
  docker exec "$DB_CONTAINER" mariadb -uroot -proot -e \
    "CREATE DATABASE IF NOT EXISTS ronda_dev CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
     GRANT ALL ON ronda_dev.* TO 'ronda'@'%'; GRANT ALL ON ronda_test.* TO 'ronda'@'%';"
fi

uv sync -q
[[ -f app/static/js/vendor/htmx.min.js ]] || ./scripts/fetch_vendor.sh
uv run alembic upgrade head

# Matikan server lama di port ini dan worker lama.
# Tanpa fuser: PID pemegang port dari `ss` (iproute2), lalu tunggu port benar-benar bebas.
kill_port() {
  local pids
  pids=$(ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u || true)
  [[ -n "$pids" ]] && kill $pids 2>/dev/null || true
  for _ in $(seq 40); do
    ss -ltnH "sport = :$1" | grep -q . || return 0
    sleep 0.25
  done
  echo "Port $1 masih dipakai proses lain (cek: ss -ltnp 'sport = :$1')."; exit 1
}
kill_port "$PORT"
pkill -f "[p]ython -m app.jobs.worker" 2>/dev/null || true
mkdir -p data

WORKER_PID=""
if [[ $WITH_WORKER == 1 ]]; then
  uv run python -m app.jobs.worker > data/worker.log 2>&1 &
  WORKER_PID=$!
  echo "Worker jalan (log: data/worker.log)"
fi
trap '[[ -n "$WORKER_PID" ]] && kill "$WORKER_PID" 2>/dev/null' EXIT

echo "Ronda di http://localhost:$PORT  (Ctrl+C untuk berhenti)"
echo "Belum ada user? uv run python -m app.auth.cli create-user admin.nama --role admin --name \"Nama\""
uv run uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload
