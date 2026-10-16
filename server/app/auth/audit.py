"""Penulis Audit Log. Dipanggil di DALAM transaksi pemilik: catatan audit ikut commit/rollback bersama aksinya."""
from dataclasses import dataclass

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuditLog


@dataclass(frozen=True)
class Actor:
    """Siapa yang melakukan aksi. user_id None = sistem (worker, agent)."""
    user_id: int | None
    username: str
    ip: str | None = None


SYSTEM = Actor(user_id=None, username="sistem")


def _clip(value: str | None, limit: int) -> str | None:
    return value[:limit] if value is not None else None


async def record(
    db: AsyncSession, actor: Actor, category: str, action: str, *, sensitive: bool = False,
    target_type: str | None = None, target_id: int | None = None, target_label: str | None = None,
    detail: str | None = None,
) -> None:
    # Core INSERT, bukan db.add(): PK (id, ts) memakai ts dari server, yang tidak perlu dibaca balik oleh ORM
    await db.execute(insert(AuditLog).values(
        user_id=actor.user_id, username=actor.username[:50], category=category, action=action[:40],
        is_sensitive=sensitive, target_type=target_type, target_id=target_id,
        target_label=_clip(target_label, 80), detail=_clip(detail, 500), ip=_clip(actor.ip, 45),
    ))
