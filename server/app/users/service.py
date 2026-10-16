from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User


async def list_users(db: AsyncSession, *, q: str | None, role: str | None, active: str | None) -> list[User]:
    stmt = select(User).order_by(func.field(User.role, "admin", "teknisi"), User.username)
    if q:
        stmt = stmt.where(or_(User.username.like(f"%{q}%"), User.full_name.like(f"%{q}%")))
    if role:
        stmt = stmt.where(User.role == role)
    if active in ("1", "0"):
        stmt = stmt.where(User.is_active.is_(active == "1"))
    return list(await db.scalars(stmt))


async def role_counts(db: AsyncSession) -> dict[str, int]:
    return dict((await db.execute(
        select(User.role, func.count()).where(User.is_active.is_(True)).group_by(User.role))).all())


def initials(full_name: str) -> str:
    parts = [p for p in full_name.split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"
