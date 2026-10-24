"""Kerja periodik domain hosts, dijalankan worker (addon jobs) tiap ±60 detik oleh satu worker (GET_LOCK)."""
import logging
import time
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.mysql import insert
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.database import db_now_plus
from app.hosts.ingest import SCREENSHOT_PICKUP_WINDOW
from app.hosts.models import Host, HostEvent, Screenshot
from app.hosts.service import mark_screenshots_failed_without_pickup
from app.jobs.queue import maintenance
from app.settings.models import Setting
from app.settings.service import get_value

logger = logging.getLogger(__name__)


@maintenance
async def mark_offline(ctx: dict) -> None:
    """PC tanpa heartbeat melewati offline_after_sec → offline + catatan di Riwayat."""
    async with ctx["db"]() as db, db.begin():
        threshold = int(await get_value(db, "offline_after_sec"))
        hosts = (await db.execute(
            select(Host.id).where(Host.status == "online", Host.last_seen_at < db_now_plus(-threshold))
            .with_for_update(skip_locked=True)   # heartbeat yang sedang mengunci baris: biarkan, berarti PC hidup
        )).scalars().all()
        if hosts:
            await db.execute(update(Host).where(Host.id.in_(hosts)).values(status="offline"))
            db.add_all(HostEvent(host_id=h, kind="status", level="crit", message="Offline (tidak ada heartbeat)")
                       for h in hosts)
    if hosts:
        logger.info("hosts_offline", extra={"count": len(hosts)})


KEEP_SCREENSHOTS_PER_HOST = 3     # gambar yang lebih lama dihapus agar disk server tidak penuh


@maintenance
async def expire_screenshots(ctx: dict) -> None:
    """Hapus file screenshot yang lewat retensi atau di luar KEEP_SCREENSHOTS_PER_HOST terbaru per PC."""
    ranked = select(
        Screenshot.id, Screenshot.file_path, Screenshot.expires_at,
        func.row_number().over(partition_by=Screenshot.host_id, order_by=Screenshot.id.desc()).label("rn"),
    ).where(Screenshot.status == "uploaded").subquery()
    async with ctx["db"]() as db:
        await mark_screenshots_failed_without_pickup(db, SCREENSHOT_PICKUP_WINDOW)
        async with db.begin():
            expired = (await db.execute(
                select(ranked.c.id, ranked.c.file_path)
                .where((ranked.c.expires_at < func.now()) | (ranked.c.rn > KEEP_SCREENSHOTS_PER_HOST)).limit(500)
            )).all()
    if not expired:
        return
    await run_in_threadpool(_delete_files, [p for _, p in expired if p])   # file dulu, lalu status
    async with ctx["db"]() as db, db.begin():
        await db.execute(update(Screenshot).where(Screenshot.id.in_([i for i, _ in expired]))
                         .values(status="expired", file_path=None))


def _delete_files(paths: list[str]) -> None:
    root = settings.SCREENSHOT_DIR.resolve()
    for relative in paths:
        target = (root / relative).resolve()
        if target.is_relative_to(root):
            target.unlink(missing_ok=True)


HOURLY_WATERMARK = "metrics_hourly_done"      # jam terakhir yang sudah diagregasi (baris di tabel settings)
MAX_HOURS_PER_RUN = 24

_AGGREGATE = text("""
INSERT INTO metrics_hourly (host_id, hour, cpu_avg, cpu_max, ram_avg, ram_max, latency_avg, latency_max,
                            net_in_avg, net_out_avg, disk_max_pct, online_min)
SELECT m.host_id, :hour, ROUND(AVG(m.cpu_pct), 2), MAX(m.cpu_pct), ROUND(AVG(m.ram_pct), 2), MAX(m.ram_pct),
       ROUND(AVG(m.latency_ms), 1), MAX(m.latency_ms), ROUND(AVG(m.net_in_bps)), ROUND(AVG(m.net_out_bps)),
       (SELECT MAX(d.used_pct) FROM disk_metrics d
         WHERE d.host_id = m.host_id AND d.ts >= :hour AND d.ts < :next_hour),
       LEAST(60, COUNT(DISTINCT FLOOR(UNIX_TIMESTAMP(m.ts) / 60)))
FROM metrics m
WHERE m.ts >= :hour AND m.ts < :next_hour
GROUP BY m.host_id
ON DUPLICATE KEY UPDATE cpu_avg = VALUES(cpu_avg), cpu_max = VALUES(cpu_max), ram_avg = VALUES(ram_avg),
  ram_max = VALUES(ram_max), latency_avg = VALUES(latency_avg), latency_max = VALUES(latency_max),
  net_in_avg = VALUES(net_in_avg), net_out_avg = VALUES(net_out_avg), disk_max_pct = VALUES(disk_max_pct),
  online_min = VALUES(online_min)
""")


@maintenance
async def aggregate_hourly(ctx: dict) -> None:
    """Agregat per jam untuk jam yang sudah lewat, mengejar ketinggalan maks. 24 jam per putaran."""
    async with ctx["db"]() as db, db.begin():
        now = await db.scalar(select(func.now()))
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        done = await db.scalar(select(Setting.value).where(Setting.name == HOURLY_WATERMARK))
    hour = datetime.fromisoformat(done) + timedelta(hours=1) if done else current_hour - timedelta(hours=1)
    for _ in range(MAX_HOURS_PER_RUN):
        if hour >= current_hour:
            break
        async with ctx["db"]() as db, db.begin():                 # satu transaksi pendek per jam
            await db.execute(_AGGREGATE, {"hour": hour, "next_hour": hour + timedelta(hours=1)})
            stmt = insert(Setting).values(name=HOURLY_WATERMARK, value=hour.isoformat())
            await db.execute(stmt.on_duplicate_key_update(value=stmt.inserted.value, updated_at=func.now()))
        hour += timedelta(hours=1)


# ---------- Partisi & retensi ----------
# (tabel, kolom, per "day"/"month", kunci retensi di settings.retention)
PARTITIONED = (
    ("metrics", "ts", "day", "metrics_days"),
    ("disk_metrics", "ts", "day", "metrics_days"),
    ("metrics_hourly", "hour", "month", "hourly_days"),
    ("audit_logs", "ts", "month", "audit_days"),
)
DAYS_AHEAD = 3
PARTITION_CHECK_INTERVAL = 3600
_last_partition_check = 0.0


def _month_start(d: date, add: int = 0) -> date:
    y, m = divmod(d.month - 1 + add, 12)
    return date(d.year + y, m + 1, 1)


def planned_partitions(unit: str, today: date) -> list[tuple[str, date]]:
    """Partisi yang harus ada mulai hari/bulan ini: (nama, batas atas eksklusif)."""
    if unit == "day":
        days = (today + timedelta(days=i) for i in range(DAYS_AHEAD + 1))
        return [(f"p{d:%Y%m%d}", d + timedelta(days=1)) for d in days]
    months = (_month_start(today, i) for i in range(2))
    return [(f"p{m:%Y%m}", _month_start(m, 1)) for m in months]


def partition_bound(name: str) -> date:
    """Batas atas partisi dari namanya: p20261014 → 2026-10-15, p202610 → 2026-11-01."""
    digits = name[1:]
    if len(digits) == 8:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:])) + timedelta(days=1)
    return _month_start(date(int(digits[:4]), int(digits[4:6]), 1), 1)


async def maintain_partitions(db_factory, *, force: bool = False) -> None:
    global _last_partition_check
    if not force and time.monotonic() - _last_partition_check < PARTITION_CHECK_INTERVAL:
        return
    async with db_factory() as db:
        async with db.begin():
            today = (await db.scalar(select(func.utc_date())))
            retention = await get_value(db, "retention")
        for table, _column, unit, retention_key in PARTITIONED:
            async with db.begin():
                existing = [r[0] for r in await db.execute(text(
                    "SELECT PARTITION_NAME FROM information_schema.PARTITIONS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND PARTITION_NAME IS NOT NULL "
                    "ORDER BY PARTITION_ORDINAL_POSITION"), {"t": table})]
            if "pmax" not in existing:
                logger.error("partition_missing_pmax", extra={"table": table})
                continue
            missing = [(n, b) for n, b in planned_partitions(unit, today) if n not in existing]
            highest = max((partition_bound(n) for n in existing if n != "pmax"), default=date.min)
            missing = [(n, b) for n, b in missing if b > highest]   # REORGANIZE hanya bisa memecah pmax
            if missing:
                parts = ", ".join(f"PARTITION {n} VALUES LESS THAN ('{b.isoformat()}')" for n, b in missing)
                # DDL = commit implisit; pmax kosong sehingga instan
                await db.execute(text(f"ALTER TABLE {table} REORGANIZE PARTITION pmax INTO "
                                      f"({parts}, PARTITION pmax VALUES LESS THAN (MAXVALUE))"))
                await db.commit()
                logger.info("partitions_added", extra={"table": table, "partitions": [n for n, _ in missing]})
            cutoff = today - timedelta(days=int(retention[retention_key]))
            old = [n for n in existing if n != "pmax" and partition_bound(n) <= cutoff]
            if old and len(old) < len(existing) - 1:            # sisakan minimal satu partisi data
                await db.execute(text(f"ALTER TABLE {table} DROP PARTITION {', '.join(old)}"))
                await db.commit()
                logger.info("partitions_dropped", extra={"table": table, "partitions": old})
    _last_partition_check = time.monotonic()


@maintenance
async def partitions(ctx: dict) -> None:
    await maintain_partitions(ctx["db"])

