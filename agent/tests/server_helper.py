"""Dijalankan dengan Python venv server (bukan venv agent) oleh test_contract.py:
aksi admin yang tidak lewat API agent — buat enrollment key, setujui/tolak PC, baca hasil, kosongkan tabel.

    server/.venv/bin/python agent/tests/server_helper.py key | approve <host_id> | reject <host_id> | stats <host_id> | truncate
"""
import asyncio
import json
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER_DIR))

from sqlalchemy import select, text  # noqa: E402

from app.agents import service as agent_service  # noqa: E402
from app.auth import service as auth_service  # noqa: E402
from app.auth.audit import SYSTEM, Actor  # noqa: E402
from app.auth.models import User  # noqa: E402
from app.hosts import service as host_service  # noqa: E402
from app.core.database import Base, async_session_factory, engine  # noqa: E402
from app.core.discovery import import_domain_modules  # noqa: E402

import_domain_modules("models")


async def _admin(db) -> Actor:
    async with db.begin():
        user = await db.scalar(select(User).where(User.username == "admin.kontrak"))
    if user is None:
        user = await auth_service.create_user(db, "admin.kontrak", "s3cret-pass-panjang", "admin")
    return Actor(user.id, user.username, "127.0.0.1")


async def main(cmd: str, *args: str) -> object:
    try:
        async with async_session_factory() as db:
            if cmd == "key":
                _, raw = await agent_service.create_enrollment_key(db, prefix="TEST", valid_days=1,
                                                                 actor=await _admin(db))
                return raw
            if cmd == "approve":
                return await agent_service.approve_hosts(db, {int(args[0]): None}, None, SYSTEM)
            if cmd == "reject":
                await agent_service.reject_host(db, int(args[0]), SYSTEM)
                return True
            if cmd == "stats":
                host_id = int(args[0])
                async with db.begin():
                    q = lambda sql: db.scalar(text(sql), {"h": host_id})  # noqa: E731
                    return {"status": await q("SELECT status FROM hosts WHERE id = :h"),
                            "hostname": await q("SELECT hostname FROM hosts WHERE id = :h"),
                            "metrics": await q("SELECT COUNT(*) FROM metrics WHERE host_id = :h"),
                            "disk_metrics": await q("SELECT COUNT(*) FROM disk_metrics WHERE host_id = :h"),
                            "latest_user": await q("SELECT logged_user FROM host_latest WHERE host_id = :h"),
                            "screenshot": await q("SELECT status FROM screenshots WHERE host_id = :h LIMIT 1")}
            if cmd == "screenshot":
                shot = await host_service.request_screenshot(
                    db, int(args[0]), reason="uji kontrak agent", notify_user=False, actor=await _admin(db),
                    threshold=120)
                return shot.request_uuid
            if cmd == "release":                      # version, path zip, signature → unggah lalu rilis sampai Ring 2
                actor = await _admin(db)
                with open(args[1], "rb") as fh:
                    release = await agent_service.upload_release(db, version=args[0], signature=args[2], notes=None,
                                                                 upload=fh, actor=actor)
                for action in ("start", "promote", "promote"):
                    await agent_service.change_release(db, release.id, action, actor)
                return release.sha256
            if cmd == "agent_version":
                async with db.begin():
                    return await db.scalar(text("SELECT CONCAT(agent_version,' ',IFNULL(update_status,'-'))"
                                                " FROM hosts WHERE id = :h"), {"h": int(args[0])})
            if cmd == "refresh_inventory":
                await host_service.request_inventory(db, int(args[0]), await _admin(db))
                return True
            if cmd == "inventory":
                h = {"h": int(args[0])}
                async with db.begin():
                    q = lambda sql: db.scalar(text(sql), h)  # noqa: E731
                    return {"os_name": await q("SELECT os_name FROM hosts WHERE id = :h"),
                            "ram_total_mb": await q("SELECT ram_total_mb FROM hosts WHERE id = :h"),
                            "disks": await q("SELECT COUNT(*) FROM host_disks WHERE host_id = :h"),
                            "adapters": await q("SELECT COUNT(*) FROM network_adapters WHERE host_id = :h"),
                            "server_route": await q("SELECT name FROM network_adapters WHERE host_id = :h"
                                                    " AND is_server_route = 1"),
                            "apps": await q("SELECT COUNT(*) FROM installed_apps WHERE host_id = :h")}
            if cmd == "truncate":
                async with engine.begin() as conn:
                    await conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
                    for table in Base.metadata.sorted_tables:
                        await conn.execute(text(f"TRUNCATE TABLE {table.name}"))
                    await conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
                return True
            raise SystemExit(f"perintah tidak dikenal: {cmd}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(*sys.argv[1:])), default=str))
