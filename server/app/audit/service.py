from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuditLog, User
from app.hosts.models import Host

PAGE_SIZE = 50
RANGES = {"today": "Hari ini", "7d": "7 hari", "30d": "30 hari", "custom": "Rentang tanggal…"}


@dataclass
class AuditFilters:
    range_key: str = "7d"
    start: datetime | None = None     # UTC
    end: datetime | None = None       # UTC, eksklusif
    username: str | None = None
    category: str | None = None
    target: str | None = None
    sensitive: bool = False
    hidden: frozenset[int] = frozenset()     # folder terbatas tanpa akses: catatan tentang folder/PC-nya disembunyikan


def _where(stmt, f: AuditFilters):
    if f.start:
        stmt = stmt.where(AuditLog.ts >= f.start)      # partisi per bulan: hanya partisi relevan yang dibaca
    if f.end:
        stmt = stmt.where(AuditLog.ts < f.end)
    if f.username:
        stmt = stmt.where(AuditLog.username == f.username)
    if f.category:
        stmt = stmt.where(AuditLog.category == f.category)
    if f.target:
        stmt = stmt.where(AuditLog.target_label.like(f"%{f.target}%"))
    if f.sensitive:
        stmt = stmt.where(AuditLog.is_sensitive.is_(True))
    if f.hidden:
        # target_type/target_id boleh NULL: tulis eksplisit agar baris tanpa target tidak ikut terbuang oleh NOT
        hidden_hosts = select(Host.id).where(Host.folder_id.in_(f.hidden))
        stmt = stmt.where(
            or_(AuditLog.target_type.is_(None), AuditLog.target_id.is_(None),
                and_(AuditLog.target_type == "host", AuditLog.target_id.not_in(hidden_hosts)),
                and_(AuditLog.target_type == "folder", AuditLog.target_id.not_in(f.hidden)),
                AuditLog.target_type.not_in(("host", "folder"))))
    return stmt


async def page(db: AsyncSession, f: AuditFilters, *, before: tuple[datetime, int] | None = None,
               limit: int = PAGE_SIZE) -> tuple[list[AuditLog], tuple[datetime, int] | None]:
    """Keyset (ts, id) menurun."""
    stmt = _where(select(AuditLog), f).order_by(AuditLog.ts.desc(), AuditLog.id.desc()).limit(limit + 1)
    if before:
        stmt = stmt.where(or_(AuditLog.ts < before[0], and_(AuditLog.ts == before[0], AuditLog.id < before[1])))
    rows = list(await db.scalars(stmt))
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, ((rows[-1].ts, rows[-1].id) if more else None)


async def export(db: AsyncSession, f: AuditFilters, limit: int = 50000) -> list[AuditLog]:
    stmt = _where(select(AuditLog), f).order_by(AuditLog.ts.desc(), AuditLog.id.desc()).limit(limit)
    return list(await db.scalars(stmt))


async def usernames(db: AsyncSession) -> list[str]:
    return [*await db.scalars(select(User.username).order_by(User.username)), "sistem"]


def local_day_bounds(start: date, end: date, to_utc) -> tuple[datetime, datetime]:
    return to_utc(datetime.combine(start, time.min)), to_utc(datetime.combine(end + timedelta(days=1), time.min))
