"""Langkah database saat deploy, dijalankan DI DALAM container web oleh deploy/upgrade.sh (lewat stdin):

    docker compose run --rm -T --no-deps -e DB_ADMIN_USER -e DB_ADMIN_PASSWORD web python - migrate < deploy/db_admin.py
    docker compose run --rm -T --no-deps -e DB_ADMIN_USER -e DB_ADMIN_PASSWORD web python - grant   < deploy/db_admin.py

Host, port, dan nama database diambil dari DATABASE_URL container (user aplikasi, .env); hanya user & password
yang diganti dengan DB_ADMIN_USER/DB_ADMIN_PASSWORD. Jadi host DB-nya sama persis dengan yang dipakai aplikasi.

- migrate: `alembic upgrade head` dengan user pemilik skema (ronda_migrate).
- grant:   hak user aplikasi (ronda_app) untuk tabel yang dibuat migrasi SETELAH setup awal, dan hak tambahan user
           worker (env WORKER_DB_USER). Butuh user dengan GRANT OPTION (root). GRANT idempoten: aman setiap deploy.
"""
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

# Hak ronda_app (user di DATABASE_URL) per tabel baru — README "Hak akses database": grant ditulis per tabel.
# Setiap migrasi yang membuat tabel baru: tambahkan barisnya di sini.
APP_TABLE_GRANTS = {
    "folder_access": "SELECT, INSERT, DELETE",       # 20261106 folder terbatas
    "agent_installers": "SELECT, INSERT",            # 20261118 installer untuk /install.sh & /install.ps1
}
# Hak user worker (WORKER_DB_USER, dari WORKER_DATABASE_URL di .env) untuk kerja periodik baru. GRANT menambah hak,
# jadi hak yang sudah ada tidak berkurang.
WORKER_TABLE_GRANTS = {
    "agent_releases": "SELECT, INSERT, UPDATE",      # 20261118 rilis dari agent/releases + naik ring otomatis
    "host_events": "SELECT, INSERT",                 # 20261118 cek rollback untuk naik ring otomatis
    "audit_logs": "SELECT, INSERT",
}


def admin_url():
    user, password = os.environ.get("DB_ADMIN_USER"), os.environ.get("DB_ADMIN_PASSWORD")
    if not user or password is None:
        sys.exit("DB_ADMIN_USER dan DB_ADMIN_PASSWORD wajib diisi.")
    return make_url(os.environ["DATABASE_URL"]).set(username=user, password=password)


def migrate() -> None:
    # Harus di-set sebelum app.core.config diimpor (migrations/env.py membaca settings.DATABASE_URL)
    os.environ["DATABASE_URL"] = admin_url().render_as_string(hide_password=False)
    from alembic import command
    from alembic.config import Config
    command.upgrade(Config("alembic.ini"), "head")


async def grant() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    app = make_url(os.environ["DATABASE_URL"])
    engine = create_async_engine(admin_url(), poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            hosts = (await conn.execute(text("SELECT Host FROM mysql.user WHERE User = :u"),
                                        {"u": app.username})).scalars().all()
            if not hosts:
                sys.exit(f"User database {app.username} tidak ditemukan.")
            tables = set((await conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = :d"),
                {"d": app.database})).scalars())
            targets = [(app.username, hosts, APP_TABLE_GRANTS)]
            worker = os.environ.get("WORKER_DB_USER")
            if worker:
                worker_hosts = (await conn.execute(text("SELECT Host FROM mysql.user WHERE User = :u"),
                                                   {"u": worker})).scalars().all()
                if worker_hosts:
                    targets.append((worker, worker_hosts, WORKER_TABLE_GRANTS))
                else:
                    print(f"  lewati user worker {worker}: tidak ditemukan")
            else:
                print("  lewati grant worker: WORKER_DB_USER kosong")
            for user, user_hosts, grants in targets:
                for table, privileges in grants.items():
                    if table not in tables:
                        print(f"  lewati {table}: tabel belum ada (migrasi belum jalan?)")
                        continue
                    for host in user_hosts:
                        await conn.execute(text(f"GRANT {privileges} ON `{app.database}`.`{table}` TO :u@:h"),
                                           {"u": user, "h": host})
                        print(f"  GRANT {privileges} ON {app.database}.{table} TO '{user}'@'{host}'")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "migrate":
        migrate()
    elif action == "grant":
        from sqlalchemy.exc import DBAPIError
        try:
            asyncio.run(grant())
        except DBAPIError as err:
            sys.exit(f"GRANT gagal: {err.orig}\nPakai user dengan GRANT OPTION (mis. root), bukan ronda_migrate.")
    else:
        sys.exit("Pakai: python - migrate|grant < deploy/db_admin.py")
